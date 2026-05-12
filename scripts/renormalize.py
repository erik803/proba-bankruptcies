"""Re-run the normalizer against every event's stored raw payload.

Computes new values in pure Python first, then issues **one bulk UPDATE per
table** — finishes in seconds instead of the per-row round-tripping the
previous version did. Tested on 703 events; runs in ~3 seconds end-to-end.

This is the payoff for keeping the original source data in
`bankruptcy_event.raw`: when normalization logic improves (entity-suffix
regex, HTML stripping, etc.), we can re-process the entire dataset
without re-fetching from CourtListener or EDGAR.

Fields touched:
  - bankruptcy_event: jurisdiction_specific, debtor_classification,
    classification_confidence, classification_method
  - debtor: name, normalized_name, entity_type (primary debtor only)

Classification upgrades made by downstream passes (cross_check,
edgar_public_company) are preserved — renormalize only restores the
*base* classification from the normalizer, not the post-pass overrides.

EDGAR `proceeding_type`, `jurisdiction_court_id`, `jurisdiction_court_name`,
and `case_number` (set by `scripts/reparse_edgar_bodies.py` from the 8-K
body) are also preserved when the previous run produced a real parse — we
don't have the body in this pass, so we'd just clobber real values with
defaults / None. Detect via `jurisdiction_specific.proceeding_type_method`
or `court_extraction_method` starting with `8k_body_`.

Usage:
    python -u scripts/renormalize.py
"""

import logging
import sys

from sqlalchemy import update
from sqlmodel import Session, select

from bankruptcy.db import engine
from bankruptcy.models import BankruptcyEvent, Debtor
from bankruptcy.normalize import (
    normalize_courtlistener_result,
    normalize_edgar_filing,
)

logger = logging.getLogger("renormalize")

NORMALIZERS = {
    "courtlistener": normalize_courtlistener_result,
    "edgar": normalize_edgar_filing,
}

# Classifications produced by downstream passes (not the normalizer).
# Renormalize must not roll these back to base values — they'd lose the
# cross-source confidence boost and the user would have to re-run the
# crosscheck pass to recover.
PRESERVED_METHODS = {"cross_check", "edgar_public_company"}


def main() -> None:
    # Force line-buffered stdout so progress shows up under `| tee`, `| tail`
    # etc. Previously this script's logs were invisible when piped.
    sys.stdout.reconfigure(line_buffering=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )

    with Session(engine) as session:
        # Pull all events and all primary debtors up front. Two queries
        # total, vs the previous version's 703 SELECTs inside the loop.
        events = session.exec(select(BankruptcyEvent)).all()
        logger.info("loaded %d events", len(events))

        primary_by_event = {
            d.event_id: d
            for d in session.exec(
                select(Debtor).where(Debtor.role == "primary")
            ).all()
        }
        logger.info("loaded %d primary debtors", len(primary_by_event))

        # Compute all new values in pure Python — no DB chatter in the loop.
        event_updates: list[dict] = []
        debtor_updates: list[dict] = []
        errors = 0
        unknown_source = 0

        for event in events:
            normalizer = NORMALIZERS.get(event.source)
            if normalizer is None:
                unknown_source += 1
                continue
            try:
                new_event, new_debtors = normalizer(event.raw)
            except Exception:
                logger.exception(
                    "normalize failed for %s/%s",
                    event.source, event.source_record_id,
                )
                errors += 1
                continue

            # Preserve cross-source classifications; otherwise overwrite with
            # whatever the current normalizer says.
            if event.classification_method in PRESERVED_METHODS:
                cls = event.debtor_classification
                conf = event.classification_confidence
                method = event.classification_method
            else:
                cls = new_event.debtor_classification
                conf = new_event.classification_confidence
                method = new_event.classification_method

            # Preserve real 8-K body parses — without the body in this pass
            # we'd clobber state-ABC detection, court_id, and case_number
            # with the normalizer's defaults. Detect via the method tags
            # we stamped into jurisdiction_specific during the body parse.
            existing_js = event.jurisdiction_specific or {}
            existing_pt_method = existing_js.get("proceeding_type_method", "")
            existing_court_method = existing_js.get("court_extraction_method", "")
            preserved_pt = (
                isinstance(existing_pt_method, str)
                and existing_pt_method.startswith("8k_body_")
            )
            preserved_court = (
                isinstance(existing_court_method, str)
                and existing_court_method.startswith("8k_body_")
                and existing_court_method != "8k_body_no_match"
            )

            if preserved_pt or preserved_court:
                merged_js = dict(new_event.jurisdiction_specific or {})
                if preserved_pt:
                    merged_js["proceeding_type_method"] = existing_pt_method
                    merged_js["proceeding_type_confidence"] = existing_js.get(
                        "proceeding_type_confidence"
                    )
                if preserved_court:
                    merged_js["court_extraction_method"] = existing_court_method
                event_updates.append({
                    "event_id": event.event_id,
                    "debtor_classification": cls,
                    "classification_confidence": conf,
                    "classification_method": method,
                    "proceeding_type": (
                        event.proceeding_type if preserved_pt else new_event.proceeding_type
                    ),
                    "jurisdiction_court_id": (
                        event.jurisdiction_court_id
                        if preserved_court else new_event.jurisdiction_court_id
                    ),
                    "jurisdiction_court_name": (
                        event.jurisdiction_court_name
                        if preserved_court else new_event.jurisdiction_court_name
                    ),
                    "case_number": (
                        event.case_number if preserved_court else new_event.case_number
                    ),
                    "jurisdiction_specific": merged_js,
                })
            else:
                event_updates.append({
                    "event_id": event.event_id,
                    "debtor_classification": cls,
                    "classification_confidence": conf,
                    "classification_method": method,
                    "jurisdiction_specific": new_event.jurisdiction_specific,
                })

            primary = primary_by_event.get(event.event_id)
            if primary and new_debtors:
                debtor_updates.append({
                    "debtor_id": primary.debtor_id,
                    "name": new_debtors[0].name,
                    "normalized_name": new_debtors[0].normalized_name,
                    "entity_type": new_debtors[0].entity_type,
                })

        logger.info(
            "computed updates: events=%d debtors=%d errors=%d unknown-source=%d",
            len(event_updates), len(debtor_updates), errors, unknown_source,
        )

        # Two bulk UPDATEs, two round-trips. SQLAlchemy compiles each into
        # a single prepared statement and binds all rows in one go.
        if event_updates:
            logger.info("applying %d event updates...", len(event_updates))
            session.execute(update(BankruptcyEvent), event_updates)
        if debtor_updates:
            logger.info("applying %d debtor updates...", len(debtor_updates))
            session.execute(update(Debtor), debtor_updates)
        session.commit()

    logger.info(
        "Done. events=%d debtors=%d errors=%d",
        len(event_updates), len(debtor_updates), errors,
    )


if __name__ == "__main__":
    main()
