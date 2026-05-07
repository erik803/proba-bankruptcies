"""FastAPI app: JSON API + HTML dashboard for the bankruptcy pilot.

Run with:
    uvicorn bankruptcy.api:app --reload
"""

from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func
from sqlalchemy.orm import selectinload
from sqlmodel import Session, select

from bankruptcy.db import get_session
from bankruptcy.models import BankruptcyEvent, Debtor
from bankruptcy.normalize import normalize_name

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

app = FastAPI(
    title="Bankruptcy Detection Pilot",
    description="US Chapter 7 / Chapter 11 bankruptcy event detection.",
    version="0.1.0",
)


# --- response models --------------------------------------------------------

class DebtorResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    debtor_id: UUID
    name: str
    normalized_name: str
    entity_type: str
    role: str
    identifiers: dict[str, Any] = Field(default_factory=dict)


class EventResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    event_id: UUID
    source: str
    source_url: str | None = None
    jurisdiction_country: str
    jurisdiction_court_id: str
    jurisdiction_court_name: str | None = None
    proceeding_type: str
    case_number: str | None = None
    pacer_case_id: str | None = None
    filed_at: date
    source_first_seen_at: datetime | None = None
    ingested_at: datetime
    status: str
    debtor_classification: str
    classification_confidence: float
    classification_method: str | None = None
    related_filing_group_id: UUID | None = None
    debtors: list[DebtorResponse] = Field(default_factory=list)
    jurisdiction_specific: dict[str, Any] = Field(default_factory=dict)


class EventListResponse(BaseModel):
    items: list[EventResponse]
    total: int
    limit: int
    offset: int


# --- routes -----------------------------------------------------------------

@app.get("/healthz", tags=["meta"])
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/bankruptcies", response_model=EventListResponse, tags=["events"])
def list_bankruptcies(
    company: str | None = Query(None, description="Substring match on debtor name (case-insensitive)"),
    date_from: date | None = Query(None, alias="from", description="filed_at >= this date"),
    date_to: date | None = Query(None, alias="to", description="filed_at <= this date"),
    court: str | None = Query(None, description="Court ID (e.g. deb, nysb)"),
    proceeding_type: str | None = Query(None, description="chapter_7, chapter_11, etc."),
    classification: str | None = Query(None, description="business | individual | unknown"),
    min_confidence: float = Query(0.0, ge=0.0, le=1.0),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    session: Session = Depends(get_session),
) -> EventListResponse:
    base = select(BankruptcyEvent)

    if date_from:
        base = base.where(BankruptcyEvent.filed_at >= date_from)
    if date_to:
        base = base.where(BankruptcyEvent.filed_at <= date_to)
    if court:
        base = base.where(BankruptcyEvent.jurisdiction_court_id == court)
    if proceeding_type:
        base = base.where(BankruptcyEvent.proceeding_type == proceeding_type)
    if classification:
        base = base.where(BankruptcyEvent.debtor_classification == classification)
    if min_confidence > 0:
        base = base.where(BankruptcyEvent.classification_confidence >= min_confidence)
    if company:
        normalized = normalize_name(company)
        debtor_subq = (
            select(Debtor.event_id)
            .where(Debtor.normalized_name.ilike(f"%{normalized}%"))
        )
        base = base.where(BankruptcyEvent.event_id.in_(debtor_subq))

    total = session.scalar(select(func.count()).select_from(base.subquery())) or 0

    listed = (
        base.options(selectinload(BankruptcyEvent.debtors))
        .order_by(BankruptcyEvent.filed_at.desc(), BankruptcyEvent.ingested_at.desc())
        .limit(limit)
        .offset(offset)
    )
    events = session.exec(listed).all()

    return EventListResponse(
        items=[EventResponse.model_validate(e) for e in events],
        total=total,
        limit=limit,
        offset=offset,
    )


@app.get("/bankruptcies/{event_id}", response_model=EventResponse, tags=["events"])
def get_bankruptcy(
    event_id: UUID,
    session: Session = Depends(get_session),
) -> EventResponse:
    event = session.exec(
        select(BankruptcyEvent)
        .options(selectinload(BankruptcyEvent.debtors))
        .where(BankruptcyEvent.event_id == event_id)
    ).first()
    if not event:
        raise HTTPException(status_code=404, detail="event not found")
    return EventResponse.model_validate(event)


@app.get("/", response_class=HTMLResponse, tags=["dashboard"])
def dashboard(request: Request, session: Session = Depends(get_session)) -> Any:
    total = session.scalar(select(func.count()).select_from(BankruptcyEvent)) or 0

    by_classification = dict(
        session.exec(
            select(BankruptcyEvent.debtor_classification, func.count())
            .group_by(BankruptcyEvent.debtor_classification)
        ).all()
    )
    by_court = dict(
        session.exec(
            select(BankruptcyEvent.jurisdiction_court_id, func.count())
            .group_by(BankruptcyEvent.jurisdiction_court_id)
            .order_by(func.count().desc())
        ).all()
    )
    by_proceeding = dict(
        session.exec(
            select(BankruptcyEvent.proceeding_type, func.count())
            .group_by(BankruptcyEvent.proceeding_type)
        ).all()
    )

    cutoff = date.today() - timedelta(days=30)
    by_day_rows = session.exec(
        select(BankruptcyEvent.filed_at, func.count())
        .where(BankruptcyEvent.filed_at >= cutoff)
        .group_by(BankruptcyEvent.filed_at)
        .order_by(BankruptcyEvent.filed_at)
    ).all()
    by_day = {str(d): n for d, n in by_day_rows}

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "total": total,
            "by_classification": by_classification,
            "by_court": by_court,
            "by_proceeding": by_proceeding,
            "by_day": by_day,
        },
    )
