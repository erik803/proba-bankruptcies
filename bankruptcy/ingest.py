"""Ingestion CLI: pull recent filings from CourtListener and upsert to DB.

Three usage shapes:

    # Per-court (pilot backfill mode): one search per (court, chapter).
    python -m bankruptcy.ingest --court deb --chapter 11 --max-per-combo 50

    # Nationwide manual window (one-shot backfills).
    python -m bankruptcy.ingest --chapter 11 --filed-after 2026-04-12

    # Steady-state (nationwide, resume from last successful poll).
    python -m bankruptcy.ingest --use-watermark

`--use-watermark` reads `ingest_watermark.last_event_date` for source
'courtlistener', queries `filed_after = last_event_date - lookback_days`
(default 7 — catches PACER backfills), runs the ingest, and writes the new
high-watermark on success. Currently opt-in; in production this should be
the default (see DECISIONS.md §4.4 / progress.md "Presentation reminders").

Idempotent: re-running on the same window will skip records already in the
DB (matched by `(source, source_record_id)`).
"""

import argparse
import asyncio
import logging
from datetime import date
from typing import Optional

from sqlmodel import Session, select

from bankruptcy.alerts import deliver_alert
from bankruptcy.config import settings
from bankruptcy.db import engine
from bankruptcy.models import BankruptcyEvent, Debtor
from bankruptcy.normalize import normalize_courtlistener_result
from bankruptcy.sources.courtlistener import CourtListenerClient
from bankruptcy.watermark import (
    compute_filed_after,
    get_watermark,
    mark_run_failed,
    update_watermark,
)

WATERMARK_SOURCE = "courtlistener"

logger = logging.getLogger("bankruptcy.ingest")


def insert_if_new(
    session: Session,
    event: BankruptcyEvent,
    debtors: list[Debtor],
) -> bool:
    """Insert event + debtors if not already present. Returns True if inserted."""
    existing = session.exec(
        select(BankruptcyEvent.event_id).where(
            BankruptcyEvent.source == event.source,
            BankruptcyEvent.source_record_id == event.source_record_id,
        )
    ).first()
    if existing:
        return False

    session.add(event)
    for debtor in debtors:
        session.add(debtor)
    session.commit()
    return True


async def ingest_filter(
    client: CourtListenerClient,
    session: Session,
    *,
    court: Optional[str],
    chapter: str,
    filed_after: Optional[str],
    filed_before: Optional[str],
    max_results: int,
    dry_run: bool,
) -> tuple[int, int, int, Optional[date]]:
    """Returns (inserted, skipped, errors, max_filed_at) for one filter combination.

    `court=None` runs a nationwide search (one API call, scans all 95 courts).
    Otherwise runs a per-court search.

    `max_filed_at` is the latest `filed_at` seen across all yielded results
    (including ones we skipped as duplicates), so callers can advance the
    watermark even when a poll returned only already-seen events.
    """
    inserted = skipped = errors = 0
    max_filed_at: Optional[date] = None

    court_label = court if court is not None else "<nationwide>"
    window = ""
    if filed_after or filed_before:
        window = f" window=[{filed_after or '*'}..{filed_before or '*'}]"
    logger.info(
        "Fetching court=%s chapter=%s%s (max=%d)",
        court_label, chapter, window, max_results,
    )
    async for result in client.search_recap(
        court=court,
        query=f"chapter:{chapter}",
        filed_after=filed_after,
        filed_before=filed_before,
        max_results=max_results,
    ):
        try:
            event, debtors = normalize_courtlistener_result(result)
        except Exception:
            logger.exception(
                "normalization error on docket_id=%s", result.get("docket_id")
            )
            errors += 1
            continue

        # Track the high-watermark across *all* yielded results, not just
        # newly-inserted ones — even duplicate skips give us evidence the
        # source has been polled up to that date.
        if max_filed_at is None or event.filed_at > max_filed_at:
            max_filed_at = event.filed_at

        if dry_run:
            primary_name = debtors[0].name if debtors else "(no debtor)"
            logger.info(
                "DRY-RUN  %-12s %-10s  %-50s  class=%-10s conf=%.2f  via=%s",
                event.case_number,
                event.proceeding_type,
                primary_name[:50],
                event.debtor_classification,
                event.classification_confidence,
                event.classification_method,
            )
            continue

        try:
            if insert_if_new(session, event, debtors):
                inserted += 1
                primary_name = debtors[0].name if debtors else "(no debtor)"
                logger.info(
                    "inserted %-12s %-10s  %s",
                    event.case_number,
                    event.proceeding_type,
                    primary_name,
                )
                if settings.alert_webhook_url:
                    try:
                        await deliver_alert(
                            session, event, debtors, settings.alert_webhook_url
                        )
                    except Exception:
                        logger.exception(
                            "alert delivery failed for %s", event.source_record_id
                        )
            else:
                skipped += 1
        except Exception:
            session.rollback()
            logger.exception("DB error on docket_id=%s", event.source_record_id)
            errors += 1

    return inserted, skipped, errors, max_filed_at


async def run(args: argparse.Namespace) -> None:
    totals = {"inserted": 0, "skipped": 0, "errors": 0}
    run_max_filed_at: Optional[date] = None

    # If no --court flag was passed, run one nationwide query per chapter.
    # [None] is the sentinel for "no court filter."
    courts: list[Optional[str]] = args.court if args.court else [None]

    # If --use-watermark, resolve the `filed_after` window from the DB.
    # `--filed-after` on the CLI overrides the watermark (useful for manual
    # backfills). Watermark mode forces nationwide (the only mode that
    # makes sense for steady-state polling — see DECISIONS.md §1.6).
    filed_after_arg = args.filed_after
    if args.use_watermark:
        if args.court:
            raise SystemExit(
                "--use-watermark is incompatible with --court; "
                "watermark mode is nationwide-only."
            )
        with Session(engine) as session:
            wm = get_watermark(session, WATERMARK_SOURCE)
            resolved = compute_filed_after(wm, WATERMARK_SOURCE)
            if filed_after_arg is None:
                filed_after_arg = resolved.isoformat()
                if wm is None:
                    logger.info(
                        "Watermark: no prior run; cold-start window from %s",
                        filed_after_arg,
                    )
                else:
                    logger.info(
                        "Watermark: resuming from %s "
                        "(last_event=%s, lookback=%d days)",
                        filed_after_arg,
                        wm.last_event_date.isoformat(),
                        wm.lookback_days,
                    )
            else:
                logger.info(
                    "Watermark: --filed-after %s overrides watermark resolution",
                    filed_after_arg,
                )

    run_failed = False
    try:
        async with CourtListenerClient(settings.courtlistener_api_token) as client:
            with Session(engine) as session:
                for court in courts:
                    for chapter in args.chapter:
                        ins, sk, er, max_seen = await ingest_filter(
                            client,
                            session,
                            court=court,
                            chapter=chapter,
                            filed_after=filed_after_arg,
                            filed_before=args.filed_before,
                            max_results=args.max_per_combo,
                            dry_run=args.dry_run,
                        )
                        totals["inserted"] += ins
                        totals["skipped"] += sk
                        totals["errors"] += er
                        if max_seen is not None and (
                            run_max_filed_at is None or max_seen > run_max_filed_at
                        ):
                            run_max_filed_at = max_seen
    except Exception:
        run_failed = True
        raise
    finally:
        if args.use_watermark and not args.dry_run:
            with Session(engine) as session:
                if run_failed:
                    mark_run_failed(session, WATERMARK_SOURCE)
                    logger.warning("Watermark: marked run as failed (high-water not advanced).")
                elif run_max_filed_at is not None:
                    update_watermark(
                        session,
                        WATERMARK_SOURCE,
                        new_event_date=run_max_filed_at,
                        event_count=totals["inserted"],
                        status="success",
                    )
                    logger.info(
                        "Watermark: advanced to %s (inserted=%d).",
                        run_max_filed_at.isoformat(),
                        totals["inserted"],
                    )
                else:
                    # No events at all — record the attempt but leave high-water alone.
                    logger.info(
                        "Watermark: no events seen this run; high-water unchanged."
                    )

    logger.info(
        "Done. inserted=%d skipped=%d errors=%d",
        totals["inserted"],
        totals["skipped"],
        totals["errors"],
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Pull recent bankruptcy filings from CourtListener and upsert into the DB.",
    )
    parser.add_argument(
        "--court",
        action="append",
        default=None,
        help=(
            "Court ID (e.g. deb, nysb, txsb). Repeatable. "
            "Omit for nationwide mode — one API call scans all 95 courts."
        ),
    )
    parser.add_argument(
        "--chapter",
        action="append",
        default=None,
        help="Chapter number (7, 11, etc). Repeatable. Defaults to both 7 and 11.",
    )
    parser.add_argument(
        "--filed-after",
        default=None,
        help="ISO date (YYYY-MM-DD). Only return filings on/after this date.",
    )
    parser.add_argument(
        "--filed-before",
        default=None,
        help="ISO date (YYYY-MM-DD). Only return filings on/before this date.",
    )
    parser.add_argument(
        "--max-per-combo",
        type=int,
        default=100,
        help="Maximum results per (court, chapter) combination.",
    )
    parser.add_argument(
        "--use-watermark",
        action="store_true",
        help=(
            "Resume from the persisted watermark for source='courtlistener'. "
            "Nationwide-only (incompatible with --court). Currently opt-in; "
            "should be default in production."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be inserted without writing to the DB.",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
    )
    args = parser.parse_args()

    if args.chapter is None:
        args.chapter = ["7", "11"]

    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    asyncio.run(run(args))


if __name__ == "__main__":
    main()
