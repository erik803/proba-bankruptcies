"""Print the raw JSON + debtor list for a specific case_number — handy for
inspecting what CourtListener actually returned vs what we stored.

Usage: python scripts/debug_one.py 26-10601
"""

import json
import sys

from sqlmodel import Session, select

from bankruptcy.db import engine
from bankruptcy.models import BankruptcyEvent, Debtor


def main(case_number: str) -> None:
    with Session(engine) as session:
        event = session.exec(
            select(BankruptcyEvent).where(BankruptcyEvent.case_number == case_number)
        ).first()
        if not event:
            print(f"No event found for case_number={case_number}")
            return

        debtors = session.exec(
            select(Debtor).where(Debtor.event_id == event.event_id)
        ).all()

        print(f"=== {case_number} === ")
        print(f"event_id        : {event.event_id}")
        print(f"source          : {event.source}")
        print(f"source_record_id: {event.source_record_id}")
        print(f"proceeding_type : {event.proceeding_type}")
        print(f"classification  : {event.debtor_classification} (conf {event.classification_confidence}, via {event.classification_method})")
        print(f"\n--- debtors ({len(debtors)}) ---")
        for d in debtors:
            print(f"  {d.role:>10s}  {d.name!r}  (entity_type={d.entity_type})")

        print("\n--- raw.party ---")
        print(json.dumps(event.raw.get("party"), indent=2))
        print("\n--- raw.caseName ---")
        print(repr(event.raw.get("caseName")))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "26-10601")
