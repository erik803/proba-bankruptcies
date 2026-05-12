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
from sqlalchemy.orm import load_only, selectinload
from sqlmodel import Session, select

from bankruptcy.db import get_session
from bankruptcy.models import BankruptcyEvent, Debtor
from bankruptcy.normalize import normalize_name

# Top-N courts to chart on the dashboard; the rest are bucketed as "other".
DASHBOARD_TOP_COURTS = 15

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
    """Full detail — used for the single-event endpoint."""
    model_config = ConfigDict(from_attributes=True)

    event_id: UUID
    source: str
    source_url: str | None = None
    jurisdiction_country: str
    jurisdiction_court_id: str | None = None
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


class EventSummary(BaseModel):
    """Lean shape for list responses — omits the heavy `jurisdiction_specific`
    JSONB (docket entries can be 50KB+ per event). Use the single-event
    endpoint to get the full detail.

    `group_size` and `cross_source_confirmed` are computed by the list
    endpoint over the event's `related_filing_group_id`: how many events
    share that group, and whether any of them come from a different source.
    Lets clients show "this is part of a 52-entity corporate filing,
    EDGAR-confirmed" without a second round-trip."""
    model_config = ConfigDict(from_attributes=True)

    event_id: UUID
    source: str
    source_url: str | None = None
    jurisdiction_court_id: str | None = None
    jurisdiction_court_name: str | None = None
    proceeding_type: str
    case_number: str | None = None
    filed_at: date
    ingested_at: datetime
    debtor_classification: str
    classification_confidence: float
    classification_method: str | None = None
    related_filing_group_id: UUID | None = None
    group_size: int = 1
    cross_source_confirmed: bool = False
    debtors: list[DebtorResponse] = Field(default_factory=list)


class EventListResponse(BaseModel):
    items: list[EventSummary]
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
    group_id: UUID | None = Query(None, description="Show only events in this corporate group"),
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
    if group_id:
        base = base.where(BankruptcyEvent.related_filing_group_id == group_id)
    if company:
        normalized = normalize_name(company)
        debtor_subq = (
            select(Debtor.event_id)
            .where(Debtor.normalized_name.ilike(f"%{normalized}%"))
        )
        base = base.where(BankruptcyEvent.event_id.in_(debtor_subq))

    total = session.scalar(select(func.count()).select_from(base.subquery())) or 0

    # `load_only` keeps the heavy `raw` and `jurisdiction_specific` JSONB
    # columns out of the SELECT — they're never used in list responses.
    # On a 100-event page this cuts payload from MBs to KBs.
    listed = (
        base.options(
            selectinload(BankruptcyEvent.debtors),
            load_only(
                BankruptcyEvent.event_id,
                BankruptcyEvent.source,
                BankruptcyEvent.source_url,
                BankruptcyEvent.jurisdiction_court_id,
                BankruptcyEvent.jurisdiction_court_name,
                BankruptcyEvent.proceeding_type,
                BankruptcyEvent.case_number,
                BankruptcyEvent.filed_at,
                BankruptcyEvent.ingested_at,
                BankruptcyEvent.debtor_classification,
                BankruptcyEvent.classification_confidence,
                BankruptcyEvent.classification_method,
                BankruptcyEvent.related_filing_group_id,
            ),
        )
        .order_by(BankruptcyEvent.filed_at.desc(), BankruptcyEvent.ingested_at.desc())
        .limit(limit)
        .offset(offset)
    )
    events = session.exec(listed).all()

    # Compute per-event group_size + cross_source_confirmed in one query.
    # Limited to the groups represented in this page, so it scales with the
    # page size, not the dataset.
    page_group_ids = {e.related_filing_group_id for e in events if e.related_filing_group_id}
    group_meta: dict[Any, tuple[int, bool]] = {}
    if page_group_ids:
        for gid, size, has_edgar in session.exec(
            select(
                BankruptcyEvent.related_filing_group_id,
                func.count(),
                func.bool_or(BankruptcyEvent.source == "edgar"),
            )
            .where(BankruptcyEvent.related_filing_group_id.in_(page_group_ids))
            .group_by(BankruptcyEvent.related_filing_group_id)
        ).all():
            group_meta[gid] = (size, bool(has_edgar))

    items: list[EventSummary] = []
    for e in events:
        s = EventSummary.model_validate(e)
        if e.related_filing_group_id and e.related_filing_group_id in group_meta:
            size, has_edgar = group_meta[e.related_filing_group_id]
            s.group_size = size
            s.cross_source_confirmed = has_edgar
        items.append(s)

    return EventListResponse(items=items, total=total, limit=limit, offset=offset)


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
    # Aggregate by court, then map IDs to friendly (short) labels. Cap to
    # the top N so the bar chart stays readable; bundle the long tail into
    # one "Other" bar instead of rendering all 77 courts as 1-pixel slivers.
    # Returned as a list of [label, count] pairs so the order (descending by
    # count) survives Jinja2's `tojson` filter, which alphabetises dict keys.
    court_name_rows = session.exec(
        select(
            BankruptcyEvent.jurisdiction_court_id,
            BankruptcyEvent.jurisdiction_court_name,
        )
        .where(BankruptcyEvent.jurisdiction_court_id.is_not(None))
        .where(BankruptcyEvent.jurisdiction_court_name.is_not(None))
        .group_by(
            BankruptcyEvent.jurisdiction_court_id,
            BankruptcyEvent.jurisdiction_court_name,
        )
    ).all()
    court_names = {cid: cname for cid, cname in court_name_rows}

    court_counts = list(session.exec(
        select(BankruptcyEvent.jurisdiction_court_id, func.count())
        .group_by(BankruptcyEvent.jurisdiction_court_id)
        .order_by(func.count().desc())
    ).all())
    top = court_counts[:DASHBOARD_TOP_COURTS]
    rest = court_counts[DASHBOARD_TOP_COURTS:]
    by_court: list[tuple[str, int]] = []
    for cid, n in top:
        full = court_names.get(cid) or cid or "(no court)"
        # Strip the redundant "United States Bankruptcy Court, " prefix and
        # any trailing punctuation CourtListener leaves on names.
        label = full.removeprefix("United States Bankruptcy Court, ").rstrip(".,; ")
        by_court.append((label, n))
    if rest:
        by_court.append((f"Other ({len(rest)} courts)", sum(n for _, n in rest)))
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

    # All courts (with friendly labels + counts) for the filter dropdown.
    all_courts: list[dict[str, Any]] = []
    for cid, n in court_counts:
        if cid is None:
            continue
        full = court_names.get(cid) or cid
        label = full.removeprefix("United States Bankruptcy Court, ").rstrip(".,; ")
        all_courts.append({"id": cid, "label": label, "count": n})
    all_courts.sort(key=lambda c: c["label"])

    # Top corporate groups: 5 biggest clusters by event count, plus enough
    # context to render a "Top groups" panel — sample debtor (shortest name
    # in the group, which usually surfaces the parent), source mix,
    # courts touched, and date range.
    group_rows = session.exec(
        select(
            BankruptcyEvent.related_filing_group_id,
            BankruptcyEvent.source,
            BankruptcyEvent.filed_at,
            BankruptcyEvent.jurisdiction_court_id,
            Debtor.name,
        )
        .join(Debtor, Debtor.event_id == BankruptcyEvent.event_id)
        .where(BankruptcyEvent.related_filing_group_id.is_not(None))
        .where(Debtor.role == "primary")
    ).all()
    groups_acc: dict[Any, dict[str, Any]] = {}
    for gid, source, filed_at, court_id, name in group_rows:
        g = groups_acc.setdefault(gid, {
            "size": 0, "sources": set(), "courts": set(),
            "first_filed": filed_at, "last_filed": filed_at, "names": [],
        })
        g["size"] += 1
        g["sources"].add(source)
        if court_id:
            g["courts"].add(court_id)
        if filed_at < g["first_filed"]:
            g["first_filed"] = filed_at
        if filed_at > g["last_filed"]:
            g["last_filed"] = filed_at
        g["names"].append(name)
    top_groups: list[dict[str, Any]] = []
    for gid, g in sorted(groups_acc.items(), key=lambda x: -x[1]["size"])[:5]:
        # Sample name: the shortest, which is usually the parent / lead.
        sample = min(g["names"], key=len) if g["names"] else "(unknown)"
        top_groups.append({
            "group_id": str(gid),
            "size": g["size"],
            "sample_name": sample,
            "courts": sorted(g["courts"]),
            "first_filed": g["first_filed"].isoformat(),
            "last_filed": g["last_filed"].isoformat(),
            "cross_source": "edgar" in g["sources"],
        })

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "total": total,
            "by_classification": by_classification,
            "by_court": by_court,
            "by_proceeding": by_proceeding,
            "by_day": by_day,
            "all_courts": all_courts,
            "top_groups": top_groups,
        },
    )
