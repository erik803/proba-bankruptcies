# Dashboard

Single HTML page served at `GET /` from the same FastAPI app as the API. Jinja2 template + Chart.js, no build step. Designed for the live demo and the reviewer walkthrough, not for general operational use.

## Page shell
- **State:** working
- **What it does:** The web page itself. Plain HTML rendered server-side, with Chart.js pulled from a CDN. No build step, no JS framework — designed to be read top to bottom and screenshotted.
- **Where:** `bankruptcy/templates/dashboard.html`

## Summary cards
- **State:** working
- **What it does:** Headline numbers at the top of the page — total events, distinct courts they came from, the Ch 11 / Ch 7 split, how many have been cross-validated against EDGAR. The "is this real" check for anyone landing on the page.

## By-day chart
- **State:** working
- **What it does:** Shows *when* bankruptcies are happening — a bar chart of filings per day. Lets you spot spikes (e.g. a corporate group filing 52 subsidiaries on the same day) at a glance.

## By-court chart
- **State:** working
- **What it does:** Shows *where* bankruptcies are happening — a bar chart of filings per court, top N. Makes it obvious that Delaware is the dominant restructuring venue and visualises the 4-court → 77-court coverage growth.

## Filterable table
- **State:** working
- **What it does:** The actual list of events. Filter by company name, date range, court, classification, minimum confidence — same filters as the API, just behind a UI. Click a row to open the full event detail.

## Groups view
- **State:** not implemented
- **What it does:** Would show corporate filings as collapsible groups instead of flat rows — e.g. the QVC group as a single expandable row showing all 17 subsidiaries plus the EDGAR confirmation, rather than 19 disconnected lines. The grouping is already in the data; this would make it visible.

## Cross-source match highlighting
- **State:** not implemented
- **What it does:** Would visually mark events confirmed by both CourtListener and EDGAR — a badge or row colour. Today you'd have to open the detail view to tell whether an event has been cross-validated.

## Screenshots
- **State:** not yet captured
- **What it does:** Needed for the README and the deck. The reviewer forms their first impression from screenshots before they read any code or run anything.

## Known gaps
- **Mobile layout.** Not optimised. Demo is desktop-only.
- **No filter for grouped vs ungrouped events.** Can't say "show me only events that are part of a corporate group." Related to the missing groups view above.
