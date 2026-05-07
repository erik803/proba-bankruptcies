"""Sanity check: connect to the configured database and report row counts.

Run after applying the migration to confirm the connection works and the
tables exist:

    python scripts/check_db.py
"""

from sqlalchemy import func
from sqlmodel import Session, select

from bankruptcy.db import engine
from bankruptcy.models import AlertDelivery, BankruptcyEvent, Debtor


def count_rows(session: Session, model: type) -> int:
    return session.scalar(select(func.count()).select_from(model)) or 0


def main() -> None:
    with Session(engine) as session:
        counts = {
            model.__tablename__: count_rows(session, model)
            for model in (BankruptcyEvent, Debtor, AlertDelivery)
        }

    print("Database connection OK.")
    for table, n in counts.items():
        print(f"  {table:20s} {n} rows")


if __name__ == "__main__":
    main()
