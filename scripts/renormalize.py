"""Re-run the normalizer against every event's stored raw payload.

This is the payoff for keeping the original source data in `bankruptcy_event.raw`
verbatim: when normalization logic improves (e.g. fixing the QVC HTML-in-caseName
bug), we can re-process the entire dataset without re-fetching from the source.

For each event we update fields the normalizer derives:
  - jurisdiction_specific
  - debtor_classification, classification_confidence, classification_method
  - the primary debtor row (name, normalized_name, entity_type)

Other fields (event_id, filed_at, source_record_id) stay put.

Usage:  python scripts/renormalize.py
"""

import logging

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


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    updated = errors = 0

    with Session(engine) as session:
        events = session.exec(select(BankruptcyEvent)).all()
        logger.info("re-normalizing %d events", len(events))

        for event in events:
            normalizer = NORMALIZERS.get(event.source)
            if not normalizer:
                logger.warning("no normalizer for source=%s, skipping", event.source)
                continue
            try:
                new_event, new_debtors = normalizer(event.raw)
            except Exception:
                logger.exception("renormalize failed for %s", event.source_record_id)
                errors += 1
                continue

            event.debtor_classification = new_event.debtor_classification
            event.classification_confidence = new_event.classification_confidence
            event.classification_method = new_event.classification_method
            event.jurisdiction_specific = new_event.jurisdiction_specific

            existing_debtors = session.exec(
                select(Debtor).where(Debtor.event_id == event.event_id)
            ).all()
            if existing_debtors and new_debtors:
                # Single primary debtor — update in place.
                primary = existing_debtors[0]
                primary.name = new_debtors[0].name
                primary.normalized_name = new_debtors[0].normalized_name
                primary.entity_type = new_debtors[0].entity_type

            session.add(event)
            updated += 1

        session.commit()

    logger.info("Done. updated=%d errors=%d", updated, errors)


if __name__ == "__main__":
    main()
