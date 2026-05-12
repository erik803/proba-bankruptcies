# Alerts

Webhook-based notification mechanism for new bankruptcy events. **Coming back to this last** — the plan is to replace what we have with something simpler (e.g. n8n) rather than build out the missing pieces ourselves.

## Webhook delivery
- **State:** working (tested only against httpbin in Phase 2)
- **What it does:** When a new bankruptcy event lands in the database, the system POSTs a JSON payload describing it — case number, debtor name, court, classification, confidence — to a URL configured in `.env`. Lets downstream systems (Veridion's CRM, a Slack channel, an email gateway, whatever) react in near-real-time without polling our API.
- **Where:** `bankruptcy/alerts.py`

## Audit log
- **State:** working
- **What it does:** Every delivery attempt is recorded — success or failure, with the HTTP status and any error message. Lets us answer *did this alert actually go out?* without trusting the live system to remember.
- **Where:** table `alert_delivery`, written from `bankruptcy/alerts.py::deliver_alert`

## Retry worker
- **State:** not implemented
- **What it does:** Would re-try alerts that failed the first time (network blip, receiver was down). Today a failed alert just sits in the audit log forever. A real operator would expect those to keep trying for a while before giving up.

## HMAC signing
- **State:** not implemented
- **What it does:** Would attach a signature to each webhook so the receiver can prove the alert actually came from us and wasn't spoofed. Necessary if a customer is going to *act* on alerts (e.g. open a case in their CRM).

## Subscription filtering
- **State:** not planned
- **What it does:** Per-customer filters — *only Chapter 11*, *only Delaware*, *only confidence ≥ 0.9*. Explicitly **out of scope** for the pilot — we won't add this even when we come back to the alert component.

## Future direction
- **State:** planned
- **What it does:** Replace the custom delivery code with a simpler off-the-shelf workflow tool — **n8n** is the front-runner. It owns the retry, scheduling, and templating concerns out of the box, so we don't have to build (or defend) any of that ourselves. Goal is less code and a clearer story, not more features.
