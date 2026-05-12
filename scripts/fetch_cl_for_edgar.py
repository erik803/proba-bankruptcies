"""Fetch CourtListener dockets for EDGAR events that have body-extracted
case numbers but no cross-source link.

When the 8-K body gave us a court_id + case_number but the CL docket isn't
in our DB yet (usually because it was filed outside our backfill window —
public-company bankruptcies often happened months before the disclosing
8-K), this script does a targeted CL search and ingests the specific
docket. Cheap: one CL search per missing event, no pagination.

After this runs, `python -m bankruptcy.crosscheck` will form the missing
links via the case-number fast-path.

Usage:
    python -u scripts/fetch_cl_for_edgar.py
    python -u scripts/fetch_cl_for_edgar.py --dry-run
"""

import argparse
import asyncio
import logging
import sys

from sqlmodel import Session, select

from bankruptcy.config import settings
from bankruptcy.db import engine
from bankruptcy.models import BankruptcyEvent
from bankruptcy.normalize import normalize_courtlistener_result
from bankruptcy.sources.courtlistener import CourtListenerClient

logger = logging.getLogger("fetch_cl_for_edgar")


def _existing_cl_docket(session: Session, court_id: str, case_number: str) -> bool:
    """True if CL already has this (court, case) tuple in our DB."""
    found = session.exec(
        select(BankruptcyEvent.event_id)
        .where(BankruptcyEvent.source == "courtlistener")
        .where(BankruptcyEvent.jurisdiction_court_id == court_id)
        .where(BankruptcyEvent.case_number == case_number)
    ).first()
    return found is not None


async def run(dry_run: bool) -> None:
    # Pull EDGAR events with body-extracted court_id + case_number but no
    # cross-source link yet. These are the candidates to pull CL counterparts for.
    with Session(engine) as session:
        candidates = session.exec(
            select(BankruptcyEvent)
            .where(BankruptcyEvent.source == "edgar")
            .where(BankruptcyEvent.related_filing_group_id.is_(None))
            .where(BankruptcyEvent.jurisdiction_court_id.is_not(None))
            .where(BankruptcyEvent.case_number.is_not(None))
        ).all()
    logger.info("found %d EDGAR events with court+case but no CL link", len(candidates))

    inserted = skipped_existing = not_found = errors = 0

    async with CourtListenerClient(settings.courtlistener_api_token) as client:
        for edgar_e in candidates:
            court = edgar_e.jurisdiction_court_id
            case = edgar_e.case_number
            logger.info(
                "looking up CL docket: court=%s case=%s (from edgar %s)",
                court, case, edgar_e.source_record_id,
            )

            # Skip if we already have it (would have been picked up by crosscheck).
            with Session(engine) as session:
                if _existing_cl_docket(session, court, case):
                    logger.info("  already in DB; skipping")
                    skipped_existing += 1
                    continue

            # Query CL specifically for this case in this court.
            hits = []
            try:
                async for r in client.search_recap(
                    court=court,
                    query=case,
                    filed_after=None,
                    filed_before=None,
                    max_results=5,
                ):
                    # CL's search returns relevance-sorted results; filter to
                    # the exact docket number we asked for. The CL search query
                    # is fuzzy text, not a primary-key lookup.
                    if (r.get("docketNumber") or "") == case:
                        hits.append(r)
            except Exception:
                logger.exception("CL query failed for court=%s case=%s", court, case)
                errors += 1
                continue

            if not hits:
                logger.warning("  no CL docket found for court=%s case=%s", court, case)
                not_found += 1
                continue

            # Take the first exact match (should be unique within a court).
            hit = hits[0]
            try:
                event, debtors = normalize_courtlistener_result(hit)
            except Exception:
                logger.exception("  normalize failed")
                errors += 1
                continue

            logger.info(
                "  found: %s '%s' filed=%s",
                hit.get("docketNumber"),
                (hit.get("caseName") or "")[:50],
                event.filed_at,
            )

            if dry_run:
                inserted += 1  # count what would have been inserted
                continue

            with Session(engine) as session:
                # Re-check inside the write transaction in case of races.
                if _existing_cl_docket(session, court, case):
                    skipped_existing += 1
                    continue
                session.add(event)
                for d in debtors:
                    session.add(d)
                session.commit()
                inserted += 1

    logger.info(
        "Done. inserted=%d already-in-db=%d not-found=%d errors=%d  (dry_run=%s)",
        inserted, skipped_existing, not_found, errors, dry_run,
    )


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
