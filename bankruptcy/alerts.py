"""Webhook alert delivery for new bankruptcy events.

POSTs a JSON payload to the configured webhook URL and records the attempt
in `alert_delivery`. The audit log is the source of truth — operators query
it to find pending or failed deliveries.

For the pilot we attempt one delivery synchronously per new event; retries
are handled by re-running ingestion (idempotent) plus a future retry worker
that can scan the pending partial index.
"""

import logging
from datetime import datetime, timezone
from typing import Any, Optional

import httpx
from sqlmodel import Session

from bankruptcy.models import AlertDelivery, BankruptcyEvent, Debtor

logger = logging.getLogger("bankruptcy.alerts")

WEBHOOK_TIMEOUT = 10.0


def build_payload(event: BankruptcyEvent, debtors: list[Debtor]) -> dict[str, Any]:
    primary = debtors[0] if debtors else None
    return {
        "event_id": str(event.event_id),
        "source": event.source,
        "source_url": event.source_url,
        "filed_at": event.filed_at.isoformat(),
        "ingested_at": event.ingested_at.isoformat() if event.ingested_at else None,
        "proceeding_type": event.proceeding_type,
        "case_number": event.case_number,
        "jurisdiction": {
            "country": event.jurisdiction_country,
            "court_id": event.jurisdiction_court_id,
            "court_name": event.jurisdiction_court_name,
        },
        "primary_debtor": (
            {"name": primary.name, "entity_type": primary.entity_type}
            if primary
            else None
        ),
        "classification": {
            "value": event.debtor_classification,
            "confidence": event.classification_confidence,
            "method": event.classification_method,
        },
    }


async def deliver_alert(
    session: Session,
    event: BankruptcyEvent,
    debtors: list[Debtor],
    webhook_url: str,
    *,
    http: Optional[httpx.AsyncClient] = None,
) -> AlertDelivery:
    """POST the alert payload and record a row in alert_delivery (success or failure)."""
    delivery = AlertDelivery(event_id=event.event_id, webhook_url=webhook_url)
    payload = build_payload(event, debtors)

    owns_http = http is None
    if owns_http:
        http = httpx.AsyncClient(timeout=WEBHOOK_TIMEOUT)

    try:
        response = await http.post(webhook_url, json=payload)
        delivery.http_status = response.status_code
        if response.is_success:
            delivery.delivered_at = datetime.now(timezone.utc)
        else:
            delivery.last_error = f"non-2xx response: {response.status_code}"
    except httpx.HTTPError as exc:
        delivery.last_error = str(exc)[:500]
    finally:
        if owns_http:
            await http.aclose()

    session.add(delivery)
    session.commit()
    return delivery
