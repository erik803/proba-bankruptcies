"""Quick look at recent ingested events — useful right after running the
ingest CLI to eyeball what landed and how it was classified.
"""

from sqlmodel import Session, select

from bankruptcy.db import engine
from bankruptcy.models import BankruptcyEvent, Debtor


def main() -> None:
    with Session(engine) as session:
        events = session.exec(
            select(BankruptcyEvent)
            .order_by(BankruptcyEvent.filed_at.desc())
            .limit(30)
        ).all()

        for e in events:
            debtors = session.exec(
                select(Debtor).where(Debtor.event_id == e.event_id)
            ).all()
            primary = debtors[0] if debtors else None
            primary_name = primary.name if primary else "(no debtor)"
            primary_type = primary.entity_type if primary else "-"

            print(
                f"{e.filed_at}  {e.case_number:>10s}  {e.proceeding_type:<10s}"
                f"  {primary_name[:42]:<42s}"
                f"  type={primary_type:<8s}"
                f"  cls={e.debtor_classification:<10s}"
                f"  conf={e.classification_confidence:.2f}"
                f"  via={e.classification_method}"
            )


if __name__ == "__main__":
    main()
