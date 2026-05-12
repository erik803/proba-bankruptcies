"""SQLModel classes mirroring the tables in `migrations/001_initial_schema.sql`."""

from datetime import date, datetime, timezone
from typing import Any, Optional
from uuid import UUID, uuid4

from sqlalchemy import Column
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, Relationship, SQLModel


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class BankruptcyEvent(SQLModel, table=True):
    __tablename__ = "bankruptcy_event"

    event_id: UUID = Field(default_factory=uuid4, primary_key=True)

    source: str
    source_record_id: str
    source_url: Optional[str] = None

    jurisdiction_country: str
    jurisdiction_court_id: Optional[str] = None
    jurisdiction_court_name: Optional[str] = None

    proceeding_type: str
    case_number: Optional[str] = None
    pacer_case_id: Optional[str] = None

    filed_at: date
    source_first_seen_at: Optional[datetime] = None
    ingested_at: datetime = Field(default_factory=utc_now)

    status: str = "filed"
    status_updated_at: datetime = Field(default_factory=utc_now)

    debtor_classification: str = "unknown"
    classification_confidence: float = 0.0
    classification_method: Optional[str] = None

    related_filing_group_id: Optional[UUID] = None

    jurisdiction_specific: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default="{}"),
    )
    raw: dict[str, Any] = Field(sa_column=Column(JSONB, nullable=False))

    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    debtors: list["Debtor"] = Relationship(
        back_populates="event",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"},
    )


class Debtor(SQLModel, table=True):
    __tablename__ = "debtor"

    debtor_id: UUID = Field(default_factory=uuid4, primary_key=True)
    event_id: UUID = Field(foreign_key="bankruptcy_event.event_id")

    name: str
    normalized_name: str
    entity_type: str = "unknown"
    identifiers: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default="{}"),
    )
    address: Optional[dict[str, Any]] = Field(default=None, sa_column=Column(JSONB))
    role: str = "primary"

    event: Optional[BankruptcyEvent] = Relationship(back_populates="debtors")


class AlertDelivery(SQLModel, table=True):
    __tablename__ = "alert_delivery"

    delivery_id: UUID = Field(default_factory=uuid4, primary_key=True)
    event_id: UUID = Field(foreign_key="bankruptcy_event.event_id")

    webhook_url: str
    attempted_at: datetime = Field(default_factory=utc_now)
    delivered_at: Optional[datetime] = None
    http_status: Optional[int] = None
    retry_count: int = 0
    last_error: Optional[str] = None


class IngestWatermark(SQLModel, table=True):
    """High-watermark for incremental polling, one row per source.

    `last_event_date` is the max `filed_at` we've successfully ingested. Next
    run queries `filed_after = last_event_date - lookback_days` to catch
    late-arriving filings (CourtListener can backfill PACER for a few days).
    Dedup via `(source, source_record_id)` UNIQUE on `bankruptcy_event` makes
    the overlap safe.

    See DECISIONS.md §1.7 for the design rationale.
    """

    __tablename__ = "ingest_watermark"

    source: str = Field(primary_key=True)
    last_event_date: date
    last_run_at: datetime = Field(default_factory=utc_now)
    last_run_status: str = "success"
    last_event_count: int = 0
    lookback_days: int = 7
