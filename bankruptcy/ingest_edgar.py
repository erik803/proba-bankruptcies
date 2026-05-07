"""Ingestion CLI for SEC EDGAR 8-K Item 1.03 filings.

Public-company bankruptcies disclose via 8-K Item 1.03 within 4 business days
of filing for bankruptcy. EDGAR is therefore a low-latency fast lane for the
public-company subset of US bankruptcies, complementing CourtListener which
covers the broader (mostly private-company) population at hours-to-day lag.

Usage:
    python -m bankruptcy.ingest_edgar --start 2026-04-01 --end 2026-05-07

Idempotent: re-runs skip records already in the DB (matched by
`(source='edgar', source_record_id=<accession>)`).
"""

import argparse
import asyncio
import logging
from datetime import date, timedelta

from sqlmodel import Session, select

from bankruptcy.alerts import deliver_alert
from bankruptcy.config import settings
from bankruptcy.db import engine
from bankruptcy.models import BankruptcyEvent, Debtor
from bankruptcy.normalize import normalize_edgar_filing
from bankruptcy.sources.edgar import EdgarClient

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

    async with EdgarClient() as client:
        with Session(engine) as session:
            async for hit in client.search_bankruptcy_8k(
                start_date=args.start,
                end_date=args.end,
                max_results=args.max_results,
            ):
                try:
                    event, debtors = normalize_edgar_filing(hit)
                except Exception:
                    logger.exception(
                        "normalization error on accession=%s", hit.get("adsh")
                    )
                    errors += 1
                    continue

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
    parser.add_argument(
        "--start",
        type=date.fromisoformat,
        default=today - timedelta(days=60),
        help="Start date (YYYY-MM-DD). Defaults to 60 days ago.",
    )
    parser.add_argument(
        "--end",
        type=date.fromisoformat,
        default=today,
        help="End date (YYYY-MM-DD). Defaults to today.",
    )
    parser.add_argument("--max-results", type=int, default=200)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
