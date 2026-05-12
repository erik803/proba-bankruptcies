"""Ingestion CLI for SEC EDGAR 8-K Item 1.03 filings.

Public-company bankruptcies disclose via 8-K Item 1.03 within 4 business days
of filing for bankruptcy. EDGAR is therefore a low-latency fast lane for the
public-company subset of US bankruptcies, complementing CourtListener which
covers the broader (mostly private-company) population at hours-to-day lag.

Two usage shapes:

    # Manual window (backfills).
    python -m bankruptcy.ingest_edgar --start 2026-04-01 --end 2026-05-07

    # Steady-state (resume from last successful poll).
    python -m bankruptcy.ingest_edgar --use-watermark

Idempotent: re-runs skip records already in the DB (matched by
`(source='edgar', source_record_id=<accession>)`).
"""

import argparse
import asyncio
import logging
from datetime import date, timedelta
from typing import Optional

from sqlmodel import Session, select

from bankruptcy.alerts import deliver_alert
from bankruptcy.config import settings
from bankruptcy.db import engine
from bankruptcy.models import BankruptcyEvent, Debtor
from bankruptcy.normalize import normalize_edgar_filing
from bankruptcy.sources.edgar import EdgarClient
from bankruptcy.watermark import (
    compute_filed_after,
    get_watermark,
    mark_run_failed,
    update_watermark,
)

WATERMARK_SOURCE = "edgar"

logger = logging.getLogger("bankruptcy.ingest_edgar")


def insert_if_new(
    session: Session,
    event: BankruptcyEvent,
    debtors: list[Debtor],
) -> bool:
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


async def run(args: argparse.Namespace) -> None:
    inserted = skipped = errors = 0
    max_filed_at: Optional[date] = None

    # Resolve the start date from the watermark when --use-watermark is set
    # AND the user hasn't explicitly passed --start. An explicit --start
    # always wins (useful for manual re-fetches / backfills).
    start_date = args.start
    if args.use_watermark and not args.start_explicit:
        with Session(engine) as session:
            wm = get_watermark(session, WATERMARK_SOURCE)
            start_date = compute_filed_after(wm, WATERMARK_SOURCE)
            if wm is None:
                logger.info(
                    "Watermark: no prior run; cold-start window from %s",
                    start_date.isoformat(),
                )
            else:
                logger.info(
                    "Watermark: resuming from %s "
                    "(last_event=%s, lookback=%d days)",
                    start_date.isoformat(),
                    wm.last_event_date.isoformat(),
                    wm.lookback_days,
                )

    run_failed = False
    try:
        async with EdgarClient() as client:
            with Session(engine) as session:
                async for hit in client.search_bankruptcy_8k(
                    start_date=start_date,
                    end_date=args.end,
                    max_results=args.max_results,
                ):
                    # Try to fetch the 8-K body so we can parse the actual
                    # proceeding type (Ch 7 vs Ch 11 vs state ABC). Best-effort:
                    # on fetch failure we still normalize, just without body
                    # context (falls back to default chapter_11).
                    body: Optional[str] = None
                    if not args.skip_body_parse:
                        accession = hit.get("adsh") or ""
                        ciks = hit.get("ciks") or []
                        _id = hit.get("_id") or ""
                        primary_doc = _id.split(":", 1)[1] if ":" in _id else None
                        if accession and ciks and primary_doc:
                            try:
                                body = await client.fetch_filing_body(
                                    accession, primary_doc, ciks[0]
                                )
                            except Exception:
                                logger.exception(
                                    "body fetch failed for accession=%s (falling back to default chapter_11)",
                                    accession,
                                )

                    try:
                        event, debtors = normalize_edgar_filing(hit, body=body)
                    except Exception:
                        logger.exception(
                            "normalization error on accession=%s", hit.get("adsh")
                        )
                        errors += 1
                        continue

                    if max_filed_at is None or event.filed_at > max_filed_at:
                        max_filed_at = event.filed_at

                    if args.dry_run:
                        logger.info(
                            "DRY-RUN  %s  %s  %-50s  tickers=%s",
                            event.filed_at,
                            event.source_record_id,
                            (debtors[0].name if debtors else "(no debtor)")[:50],
                            (debtors[0].identifiers.get("tickers") if debtors else None),
                        )
                        continue

                    try:
                        if insert_if_new(session, event, debtors):
                            inserted += 1
                            logger.info(
                                "inserted %s  %-50s  tickers=%s",
                                event.filed_at,
                                (debtors[0].name if debtors else "(no debtor)")[:50],
                                (debtors[0].identifiers.get("tickers") if debtors else None),
                            )
                            if settings.alert_webhook_url:
                                try:
                                    await deliver_alert(
                                        session,
                                        event,
                                        debtors,
                                        settings.alert_webhook_url,
                                    )
                                except Exception:
                                    logger.exception(
                                        "alert delivery failed for %s",
                                        event.source_record_id,
                                    )
                        else:
                            skipped += 1
                    except Exception:
                        session.rollback()
                        logger.exception(
                            "DB error on accession=%s", event.source_record_id
                        )
                        errors += 1
    except Exception:
        run_failed = True
        raise
    finally:
        if args.use_watermark and not args.dry_run:
            with Session(engine) as session:
                if run_failed:
                    mark_run_failed(session, WATERMARK_SOURCE)
                    logger.warning("Watermark: marked run as failed (high-water not advanced).")
                elif max_filed_at is not None:
                    update_watermark(
                        session,
                        WATERMARK_SOURCE,
                        new_event_date=max_filed_at,
                        event_count=inserted,
                        status="success",
                    )
                    logger.info(
                        "Watermark: advanced to %s (inserted=%d).",
                        max_filed_at.isoformat(),
                        inserted,
                    )
                else:
                    logger.info(
                        "Watermark: no events seen this run; high-water unchanged."
                    )

    logger.info(
        "Done. inserted=%d skipped=%d errors=%d",
        inserted,
        skipped,
        errors,
    )


def main() -> None:
    today = date.today()
    parser = argparse.ArgumentParser(
        description="Pull 8-K Item 1.03 (bankruptcy) filings from SEC EDGAR.",
    )
    # Use `None` as the default sentinel so we can tell whether the user
    # passed --start explicitly. If they did, --use-watermark won't override it.
    parser.add_argument(
        "--start",
        type=date.fromisoformat,
        default=None,
        help="Start date (YYYY-MM-DD). Defaults to 60 days ago (or watermark when --use-watermark).",
    )
    parser.add_argument(
        "--end",
        type=date.fromisoformat,
        default=today,
        help="End date (YYYY-MM-DD). Defaults to today.",
    )
    parser.add_argument("--max-results", type=int, default=200)
    parser.add_argument(
        "--use-watermark",
        action="store_true",
        help=(
            "Resume from the persisted watermark for source='edgar'. "
            "Ignored if --start is explicitly passed. Currently opt-in; "
            "should be default in production."
        ),
    )
    parser.add_argument(
        "--skip-body-parse",
        action="store_true",
        help=(
            "Skip the 8-K body fetch / chapter extraction. All EDGAR events "
            "default to chapter_11. Useful for fast bulk fetches when you "
            "plan to re-run the body parser later."
        ),
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    args.start_explicit = args.start is not None
    if args.start is None:
        args.start = today - timedelta(days=60)

    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
