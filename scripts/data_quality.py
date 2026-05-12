"""One-shot data quality / sanity check.

Surveys the current state of the DB and surfaces anything that looks weird:
counts by source/chapter/classification, confidence histogram, the largest
clustered groups, sample 'unknown' classifications, suspicious patterns
(empty names, HTML leakage, duplicate-looking events).

Read-only. No API calls.
"""

from collections import Counter

from sqlmodel import Session, func, select

from bankruptcy.db import engine
from bankruptcy.models import AlertDelivery, BankruptcyEvent, Debtor


def section(title: str) -> None:
    print()
    print("=" * 72)
    print(title)
    print("=" * 72)


def main() -> None:
    with Session(engine) as s:
        # ----- 1. Top-level counts -----
        section("1. Top-level counts")
        total = s.exec(select(func.count()).select_from(BankruptcyEvent)).first()
        print(f"  Events:   {total}")
        print(f"  Debtors:  {s.exec(select(func.count()).select_from(Debtor)).first()}")
        print(f"  Alerts:   {s.exec(select(func.count()).select_from(AlertDelivery)).first()}")

        by_source = s.exec(
            select(BankruptcyEvent.source, func.count())
            .group_by(BankruptcyEvent.source)
        ).all()
        print(f"\n  By source:")
        for src, n in by_source:
            print(f"    {src:<15} {n}")

        by_chapter = s.exec(
            select(BankruptcyEvent.proceeding_type, func.count())
            .group_by(BankruptcyEvent.proceeding_type)
        ).all()
        print(f"\n  By proceeding_type:")
        for ch, n in by_chapter:
            print(f"    {ch:<15} {n}")

        # ----- 2. Classification & confidence -----
        section("2. Classification breakdown")
        by_class = s.exec(
            select(BankruptcyEvent.debtor_classification, func.count())
            .group_by(BankruptcyEvent.debtor_classification)
        ).all()
        for cls, n in sorted(by_class, key=lambda x: -x[1]):
            pct = n / total * 100
            print(f"  {cls or '(null)':<12} {n:>4}  ({pct:.1f}%)")

        by_method = s.exec(
            select(BankruptcyEvent.classification_method, func.count())
            .group_by(BankruptcyEvent.classification_method)
        ).all()
        print(f"\n  By method:")
        for m, n in sorted(by_method, key=lambda x: -x[1]):
            print(f"    {m or '(null)':<22} {n}")

        # Confidence histogram
        events = s.exec(select(BankruptcyEvent.classification_confidence)).all()
        buckets = Counter()
        for c in events:
            if c is None:
                buckets["null"] += 1
            else:
                buckets[f"{int(c*10)/10:.1f}"] += 1
        print(f"\n  Confidence histogram:")
        for key in sorted(buckets.keys()):
            n = buckets[key]
            bar = "#" * min(40, n // 5)
            print(f"    {key:>5}  {n:>4}  {bar}")

        # ----- 3. Court distribution -----
        section("3. Court distribution")
        by_court = s.exec(
            select(BankruptcyEvent.jurisdiction_court_id, func.count())
            .group_by(BankruptcyEvent.jurisdiction_court_id)
        ).all()
        print(f"  Distinct courts: {len(by_court)}")
        print(f"\n  Top 15:")
        for ct, n in sorted(by_court, key=lambda x: -x[1])[:15]:
            label = ct or "(null/EDGAR)"
            print(f"    {label:<10} {n}")

        # ----- 4. Date coverage -----
        section("4. Date coverage")
        dates = s.exec(select(BankruptcyEvent.filed_at)).all()
        dates = [d for d in dates if d is not None]
        if dates:
            print(f"  Earliest filed_at: {min(dates)}")
            print(f"  Latest   filed_at: {max(dates)}")
            # Histogram by day
            by_day = Counter(dates)
            top_days = sorted(by_day.items(), key=lambda x: -x[1])[:10]
            print(f"\n  Top 10 busiest filing days:")
            for d, n in top_days:
                print(f"    {d}  {n}")

        # ----- 5. Clustering -----
        section("5. Largest clustered groups")
        groups = s.exec(
            select(
                BankruptcyEvent.related_filing_group_id,
                func.count(),
            )
            .where(BankruptcyEvent.related_filing_group_id.is_not(None))
            .group_by(BankruptcyEvent.related_filing_group_id)
        ).all()
        groups_sorted = sorted(groups, key=lambda x: -x[1])
        print(f"  Total grouped events: {sum(n for _, n in groups)}")
        print(f"  Number of groups:     {len(groups)}")
        print(f"\n  Top 8 groups (size: court / date / primary debtor sample):")
        for gid, n in groups_sorted[:8]:
            members = s.exec(
                select(BankruptcyEvent)
                .where(BankruptcyEvent.related_filing_group_id == gid)
            ).all()
            members = list(members)
            courts_in_group = {m.jurisdiction_court_id for m in members}
            sources_in_group = {m.source for m in members}
            dates_in_group = {m.filed_at for m in members}
            # Sample first 3 debtor names
            sample_names = []
            for m in members[:3]:
                d = s.exec(
                    select(Debtor)
                    .where(Debtor.event_id == m.event_id)
                    .where(Debtor.role == "primary")
                ).first()
                if d:
                    sample_names.append(d.name[:38])
            print(f"\n  size={n} courts={sorted(courts_in_group)} sources={sorted(sources_in_group)}")
            print(f"    dates={sorted(dates_in_group)}")
            for nm in sample_names:
                print(f"    - {nm}")

        # ----- 6. Sample 'unknown' classifications -----
        section("6. Sample 'unknown' classifications (10 random)")
        unknowns = s.exec(
            select(BankruptcyEvent)
            .where(BankruptcyEvent.debtor_classification == "unknown")
            .limit(10)
        ).all()
        for e in unknowns:
            d = s.exec(
                select(Debtor)
                .where(Debtor.event_id == e.event_id)
                .where(Debtor.role == "primary")
            ).first()
            name = d.name if d else "(no debtor)"
            print(f"  {e.jurisdiction_court_id or '-':<7} {e.proceeding_type:<11} {name[:55]}")

        # ----- 7. Suspicious patterns -----
        section("7. Suspicious patterns")
        # Empty / null debtor names
        empty = s.exec(
            select(func.count()).select_from(Debtor)
            .where((Debtor.name == "") | (Debtor.name.is_(None)))
        ).first()
        print(f"  Debtors with empty/null name: {empty}")
        # HTML leakage in names
        html_leak = s.exec(
            select(func.count()).select_from(Debtor)
            .where(Debtor.name.contains("<"))
        ).first()
        print(f"  Debtors with '<' in name (HTML leak?): {html_leak}")
        # 'Trustee' as debtor (the old caseName bug)
        trustee = s.exec(
            select(func.count()).select_from(Debtor)
            .where(Debtor.name.ilike("%trustee%"))
            .where(Debtor.role == "primary")
        ).first()
        print(f"  Primary debtors with 'trustee' in name: {trustee}")
        # Confidence out of range
        bad_conf = s.exec(
            select(func.count()).select_from(BankruptcyEvent)
            .where(
                (BankruptcyEvent.classification_confidence < 0)
                | (BankruptcyEvent.classification_confidence > 1)
            )
        ).first()
        print(f"  Events with confidence outside [0,1]: {bad_conf}")
        # Events without a primary debtor
        events_with_primary = s.exec(
            select(func.count(func.distinct(Debtor.event_id)))
            .where(Debtor.role == "primary")
        ).first()
        print(f"  Events with no primary debtor: {total - events_with_primary}")

        # ----- 8. Joint administration -----
        section("8. Joint administration flag")
        joint = s.exec(
            select(func.count()).select_from(BankruptcyEvent)
            .where(BankruptcyEvent.jurisdiction_specific["joint_administration"].astext == "true")
        ).first()
        print(f"  Events flagged jointly administered: {joint}")

        # ----- 9. EDGAR events -----
        section("9. EDGAR events")
        edgar = s.exec(
            select(BankruptcyEvent)
            .where(BankruptcyEvent.source == "edgar")
        ).all()
        print(f"  Total: {len(edgar)}")
        for e in edgar:
            d = s.exec(
                select(Debtor)
                .where(Debtor.event_id == e.event_id)
                .where(Debtor.role == "primary")
            ).first()
            grouped = e.related_filing_group_id is not None
            print(
                f"  {e.filed_at}  conf={e.classification_confidence:.2f} "
                f"grouped={grouped}  {d.name[:40] if d else '(no debtor)':<40}  "
                f"tickers={d.identifiers.get('tickers') if d and d.identifiers else None}"
            )


if __name__ == "__main__":
    main()
