"""Re-parse existing EDGAR events with full 8-K body extraction.

Fetches the 8-K body for every `source='edgar'` row and re-runs both
extractors against it:
  - proceeding_type (chapter_7/11/13/15 or 'other' for state proceedings)
  - jurisdiction_court_id, jurisdiction_court_name, case_number

Bulk-UPDATEs the events table. Use this once after deploying body-parse
changes to backfill existing rows — going forward, `ingest_edgar.py`
fetches the body inline at ingest.

Usage:
    python -u scripts/reparse_edgar_bodies.py
    python -u scripts/reparse_edgar_bodies.py --dry-run
"""

import argparse
import asyncio
import logging
import sys

from sqlalchemy import update
from sqlmodel import Session, select

from bankruptcy.db import engine
from bankruptcy.models import BankruptcyEvent
from bankruptcy.normalize import (
    extract_court_and_case_from_8k_body,
    extract_proceeding_type_from_8k_body,
)
from bankruptcy.sources.edgar import EdgarClient

logger = logging.getLogger("reparse_edgar_bodies")


async def run(dry_run: bool) -> None:
    with Session(engine) as session:
        events = session.exec(
            select(BankruptcyEvent).where(BankruptcyEvent.source == "edgar")
        ).all()
    logger.info("loaded %d EDGAR events", len(events))

    updates: list[dict] = []
    skipped = errors = 0

    async with EdgarClient() as client:
        for event in events:
            raw = event.raw or {}
            accession = raw.get("adsh")
            ciks = raw.get("ciks") or []
            _id = raw.get("_id") or ""
            primary_doc = _id.split(":", 1)[1] if ":" in _id else None

            if not (accession and ciks and primary_doc):
                logger.warning(
                    "skipping %s: missing accession/cik/primary_doc in raw",
                    event.source_record_id,
                )
                skipped += 1
                continue

            try:
                body = await client.fetch_filing_body(
                    accession, primary_doc, ciks[0]
                )
            except Exception:
                logger.exception("body fetch failed for %s", accession)
                errors += 1
                continue

            if body is None:
                logger.warning("404 on body fetch for %s", accession)
                skipped += 1
                continue

            # Proceeding type
            pt, pt_conf, pt_method = extract_proceeding_type_from_8k_body(body)
            new_pt = pt if pt is not None else "chapter_11"

            # Court + case
            court_id, court_name, case_num, court_method = (
                extract_court_and_case_from_8k_body(body)
            )

            new_js = dict(event.jurisdiction_specific or {})
            new_js["proceeding_type_method"] = pt_method
            new_js["proceeding_type_confidence"] = pt_conf
            new_js["court_extraction_method"] = court_method

            # Prefer body-extracted values, but don't clobber existing values
            # with None — cross-check may have already filled these in from
            # CL. The body parse is authoritative when it fires; otherwise
            # keep what we have.
            updated_court_id = court_id if court_id else event.jurisdiction_court_id
            updated_court_name = court_name if court_name else event.jurisdiction_court_name
            updated_case = case_num if case_num else event.case_number

            display = raw.get("display_names", ["?"])[0][:38]
            logger.info(
                "%s  %-40s  ch=%-10s court=%-6s case=%-12s method=%s/%s",
                accession,
                display,
                new_pt,
                str(updated_court_id) if updated_court_id else "-",
                str(updated_case) if updated_case else "-",
                pt_method,
                court_method,
            )

            updates.append({
                "event_id": event.event_id,
                "proceeding_type": new_pt,
                "jurisdiction_court_id": updated_court_id,
                "jurisdiction_court_name": updated_court_name,
                "case_number": updated_case,
                "jurisdiction_specific": new_js,
            })

    logger.info(
        "computed updates: %d  skipped: %d  errors: %d",
        len(updates), skipped, errors,
    )

    if dry_run:
        logger.info("dry-run: not applying updates.")
        return

    if updates:
        with Session(engine) as session:
            session.execute(update(BankruptcyEvent), updates)
            session.commit()
        logger.info("applied %d updates.", len(updates))


def main() -> None:
    sys.stdout.reconfigure(line_buffering=True)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )

    asyncio.run(run(dry_run=args.dry_run))


if __name__ == "__main__":
    main()
