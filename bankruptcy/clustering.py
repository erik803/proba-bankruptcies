"""Cluster related bankruptcy filings into corporate-group events.

A "related filing group" is two or more cases filed by entities of the same
corporate group, often as a coordinated bankruptcy of parent + subsidiaries.
In CourtListener data these arrive as separate dockets, identifiable by:

  - Same court
  - Same filing date
  - Consecutive (or near-consecutive) case numbers

This pass assigns a shared `related_filing_group_id` to any unclustered run
of two or more such events. Already-clustered events are not touched, so the
pass is idempotent: re-running it only affects new events.

Limitation worth flagging in the deck: cross-day corporate filings (e.g.
Freedom Forever LLC on 2026-04-15, then its subsidiaries Freedom Forever
Pennsylvania/Procurement on 2026-05-02) are NOT clustered by this signal —
they're related by name token overlap, which is a v2 feature.

Usage:  python -m bankruptcy.clustering
"""

import logging
import re
from collections import defaultdict
from typing import Optional
from uuid import uuid4

from sqlmodel import Session, select

from bankruptcy.db import engine
from bankruptcy.models import BankruptcyEvent

logger = logging.getLogger("bankruptcy.clustering")

# Maximum gap between consecutive case-number sequence values to still count as
# part of the same run. 1 = strictly consecutive; 2 tolerates a single
# unrelated filing slipping between related ones.
MAX_CONSECUTIVE_GAP = 2

# Only events meeting this confidence threshold for *business* classification
# are eligible for clustering. Without this filter, busy court days produce
# false-positive groups of unrelated individual filers (e.g. a Riverside
# C.D. Cal day with 13 sequential personal Ch 7 cases). Joint individual
# petitions (married couples) are excluded by design — they need name-token
# overlap to identify, which is v2 work.
MIN_BUSINESS_CONFIDENCE = 0.7


def parse_case_sequence(case_number: Optional[str]) -> Optional[int]:
    """Extract the per-year sequence number from a case_number string.

    Court formats vary:
      - D. Delaware:    '26-10653'        -> 10653
      - S.D.N.Y.:       '26-35494'        -> 35494
      - C.D. California:'2:26-bk-14467'   -> 14467
    The trailing integer is the sequence in every format we've seen.
    """
    if not case_number:
        return None
    matches = re.findall(r"\d+", case_number)
    return int(matches[-1]) if matches else None


def _assign_group(events: list[BankruptcyEvent], session: Session, signal: str) -> None:
    group_id = uuid4()
    for e in events:
        e.related_filing_group_id = group_id
        session.add(e)
    logger.info(
        "group %s | %s %s × %d via %s: %s",
        str(group_id)[:8],
        events[0].jurisdiction_court_id,
        events[0].filed_at,
        len(events),
        signal,
        ", ".join(e.case_number for e in events),
    )


def cluster_related_filings(session: Session) -> tuple[int, int]:
    """Group related filings using two signals, in order of strength.

    Pass 1 — explicit joint-administration flag from CourtListener. When
    multiple events on the same court+date carry `joint_administration=True`
    in jurisdiction_specific, they are by definition a coordinated group.
    No false positives possible.

    Pass 2 — consecutive same-court same-date business filings that didn't
    get caught by Pass 1. Only events classified as 'business' with
    confidence >= MIN_BUSINESS_CONFIDENCE are eligible (busy court days
    produce false-positive groups of unrelated individual filers).

    Returns (groups_created, events_assigned).
    """
    groups_created = 0
    events_assigned = 0

    # --- Pass 1: explicit joint_administration flag ---
    joint_buckets: dict[tuple[str, object], list[BankruptcyEvent]] = defaultdict(list)
    candidates = session.exec(
        select(BankruptcyEvent).where(BankruptcyEvent.related_filing_group_id.is_(None))
    ).all()
    for e in candidates:
        if (e.jurisdiction_specific or {}).get("joint_administration"):
            joint_buckets[(e.jurisdiction_court_id, e.filed_at)].append(e)

    for events in joint_buckets.values():
        if len(events) < 2:
            continue
        events.sort(key=lambda x: parse_case_sequence(x.case_number) or 0)
        _assign_group(events, session, "joint_admin_flag")
        groups_created += 1
        events_assigned += len(events)

    session.flush()

    # --- Pass 2: consecutive case numbers among business-classified events ---
    unclustered = session.exec(
        select(BankruptcyEvent)
        .where(BankruptcyEvent.related_filing_group_id.is_(None))
        .where(BankruptcyEvent.debtor_classification == "business")
        .where(BankruptcyEvent.classification_confidence >= MIN_BUSINESS_CONFIDENCE)
        .order_by(
            BankruptcyEvent.jurisdiction_court_id,
            BankruptcyEvent.filed_at,
        )
    ).all()

    by_bucket: dict[tuple[str, object], list[tuple[int, BankruptcyEvent]]] = defaultdict(list)
    for e in unclustered:
        seq = parse_case_sequence(e.case_number)
        if seq is None:
            continue
        by_bucket[(e.jurisdiction_court_id, e.filed_at)].append((seq, e))

    for seq_events in by_bucket.values():
        if len(seq_events) < 2:
            continue
        seq_events.sort(key=lambda x: x[0])

        runs: list[list[BankruptcyEvent]] = []
        current: list[BankruptcyEvent] = [seq_events[0][1]]
        prev_seq = seq_events[0][0]
        for seq, event in seq_events[1:]:
            if seq - prev_seq <= MAX_CONSECUTIVE_GAP:
                current.append(event)
            else:
                if len(current) >= 2:
                    runs.append(current)
                current = [event]
            prev_seq = seq
        if len(current) >= 2:
            runs.append(current)

        for run in runs:
            _assign_group(run, session, "consecutive_case_numbers")
            groups_created += 1
            events_assigned += len(run)

    session.commit()
    return groups_created, events_assigned


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    with Session(engine) as session:
        groups, events = cluster_related_filings(session)
    logger.info("Done. groups=%d events_assigned=%d", groups, events)


if __name__ == "__main__":
    main()
