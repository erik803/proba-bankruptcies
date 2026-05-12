"""Persistent high-watermark for incremental polling.

Each ingest CLI (CourtListener, EDGAR) uses one row in `ingest_watermark`,
keyed by `source`. On `--use-watermark`:

    1. Read the current watermark for the source (`get_watermark`).
    2. Compute the next query window: `filed_after = last_event_date - lookback_days`.
       The overlap catches late-arriving filings (PACER backfills, etc.) —
       safe because `(source, source_record_id)` is UNIQUE on bankruptcy_event,
       so re-fetched rows are skipped, not duplicated.
    3. Run the ingest.
    4. On success, write the new high-watermark (`update_watermark`).

This is the industry-standard pattern for incremental ETL from sources you
don't own (Airbyte / Fivetran call it "cursor field with sync mode =
incremental + dedupe"). See DECISIONS.md §1.7.

The CLI default is opt-in (`--use-watermark`); production should flip this
to opt-out — see DECISIONS.md §4.4 and progress.md "Presentation reminders".
"""

from datetime import date, timedelta
from typing import Optional

from sqlmodel import Session, select

from bankruptcy.models import IngestWatermark, utc_now

# Default lookback windows (overridable per-row in the DB by editing
# `lookback_days`).
DEFAULT_LOOKBACK_DAYS = {
    "courtlistener": 7,   # PACER backfills can land days late
    "edgar": 2,            # 8-Ks are immutable once filed; tiny overlap for safety
}


def get_watermark(session: Session, source: str) -> Optional[IngestWatermark]:
    """Return the current watermark row for `source`, or None if never run."""
    return session.exec(
        select(IngestWatermark).where(IngestWatermark.source == source)
    ).first()


def compute_filed_after(
    watermark: Optional[IngestWatermark],
    source: str,
    fallback_days: int = 30,
) -> date:
    """Compute the `filed_after` date for the next poll.

    If a watermark exists, returns `last_event_date - lookback_days`.
    If not (first run), returns `today - fallback_days` so we have a sane
    cold-start window instead of pulling the entire history of the source.
    """
    if watermark is not None:
        return watermark.last_event_date - timedelta(days=watermark.lookback_days)
    return date.today() - timedelta(days=fallback_days)


def update_watermark(
    session: Session,
    source: str,
    *,
    new_event_date: date,
    event_count: int,
    status: str = "success",
) -> None:
    """Upsert the watermark row for `source`.

    `new_event_date` should be the max `filed_at` of events seen this run.
    Caller is responsible for committing the session.

    Defensive clamp: a future-dated event would pollute the watermark and
    break the next poll (`filed_after = 2079 - 7 days` returns nothing).
    CourtListener occasionally has garbage filed_at values (e.g. 2079 or
    2029-01-01 placeholders); we cap the watermark at today rather than
    let one bad row blow up the whole pipeline.
    """
    today = date.today()
    if new_event_date > today:
        new_event_date = today

    existing = get_watermark(session, source)
    if existing is None:
        existing = IngestWatermark(
            source=source,
            last_event_date=new_event_date,
            last_run_at=utc_now(),
            last_run_status=status,
            last_event_count=event_count,
            lookback_days=DEFAULT_LOOKBACK_DAYS.get(source, 7),
        )
        session.add(existing)
    else:
        # Never move the watermark backwards — if a run sees only old events,
        # keep the previous high-water value. (Defensive: shouldn't happen with
        # forward-only sources, but cheap insurance.)
        if new_event_date > existing.last_event_date:
            existing.last_event_date = new_event_date
        existing.last_run_at = utc_now()
        existing.last_run_status = status
        existing.last_event_count = event_count
        session.add(existing)
    session.commit()


def mark_run_failed(session: Session, source: str) -> None:
    """Record a failed run without advancing `last_event_date`.

    Used when ingest crashes — we want operators to see "last run failed" in
    the audit row but we don't want to lose the high-watermark.
    """
    existing = get_watermark(session, source)
    if existing is None:
        # Nothing to update — never had a successful run to record.
        return
    existing.last_run_at = utc_now()
    existing.last_run_status = "failed"
    session.add(existing)
    session.commit()
