"""Ingestion CLI: pull recent filings from CourtListener and upsert to DB.

Two modes:

    # Per-court (pilot backfill mode): one search per (court, chapter).
    python -m bankruptcy.ingest --court deb --chapter 11 --max-per-combo 50

    # Nationwide (production / steady-state mode): one search per chapter,
    # no court filter. Scans all 95 courts in a single API call — see
    # DECISIONS §1.6 for the rate-limit math.
    python -m bankruptcy.ingest --chapter 11 --filed-after 2026-04-12

Idempotent: re-running on the same window will skip records already in the
DB (matched by `(source, source_record_id)`).
"""

import argparse
import asyncio
import logging
from typing import Optional

from sqlmodel import Session, select

from bankruptcy.alerts import deliver_alert
from bankruptcy.config import settings
from bankruptcy.db import engine
from bankruptcy.models import BankruptcyEvent, Debtor
from bankruptcy.normalize import normalize_courtlistener_result
from bankruptcy.sources.courtlistener import CourtListenerClient

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
) -> tuple[int, int, int]:
    """Returns (inserted, skipped, errors) for one filter combination.

    `court=None` runs a nationwide search (one API call, scans all 95 courts).
    Otherwise runs a per-court search.
    """
    inserted = skipped = errors = 0

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

    return inserted, skipped, errors


async def run(args: argparse.Namespace) -> None:
    totals = {"inserted": 0, "skipped": 0, "errors": 0}

    # If no --court flag was passed, run one nationwide query per chapter.
    # [None] is the sentinel for "no court filter."
    courts: list[Optional[str]] = args.court if args.court else [None]

    async with CourtListenerClient(settings.courtlistener_api_token) as client:
        with Session(engine) as session:
            for court in courts:
                for chapter in args.chapter:
                    ins, sk, er = await ingest_filter(
                        client,
                        session,
                        court=court,
                        chapter=chapter,
                        filed_after=args.filed_after,
                        filed_before=args.filed_before,
                        max_results=args.max_per_combo,
                        dry_run=args.dry_run,
                    )
                    totals["inserted"] += ins
                    totals["skipped"] += sk
                    totals["errors"] += er

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
