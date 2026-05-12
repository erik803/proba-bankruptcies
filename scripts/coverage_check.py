"""Volume + quality check across CourtListener courts.

Two questions this answers:

1. **Volume check** — what share of nationwide US bankruptcy filings (by
   chapter) happen in the 4 courts we backfilled (deb, nysb, txsb, cacb)
   versus the other 91? Hits the CL search endpoint with and without a
   `court` filter and reads the `count` field from the response.

2. **Quality check** — when we sample a few "small" districts that we
   didn't backfill, what's the business-vs-individual classification mix?
   This tells us whether the other 91 courts are mostly individuals (in
   which case our 4-court backfill is a defensible focus) or whether
   we're systematically missing business filings.

Run:
    python scripts/coverage_check.py

Read-only: no DB writes. Uses the existing normalizer for the quality
sample so the classification logic matches production.
"""

import asyncio
from collections import Counter
from datetime import date, timedelta

import httpx
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from bankruptcy.config import settings
from bankruptcy.normalize import normalize_courtlistener_result
from bankruptcy.sources.courtlistener import INTER_PAGE_SLEEP_S, is_retryable


@retry(
    retry=retry_if_exception(is_retryable),
    wait=wait_exponential(multiplier=2, min=20, max=120),
    stop=stop_after_attempt(8),
    reraise=True,
)
async def _get(http: httpx.AsyncClient, url: str, params: dict | None) -> dict:
    headers = {"Authorization": f"Token {settings.courtlistener_api_token}"}
    r = await http.get(url, params=params, headers=headers, timeout=30.0)
    r.raise_for_status()
    return r.json()

BASE = "https://www.courtlistener.com/api/rest/v4/search/"

OUR_COURTS = ["deb", "nysb", "txsb", "cacb"]
SAMPLE_COURTS = ["nvb", "flsb", "ilnb"]  # Nevada, Florida-S (Miami), Illinois-N (Chicago)
CHAPTERS = ["7", "11"]

# 30-day window ending today.
WINDOW_END = date.today()
WINDOW_START = WINDOW_END - timedelta(days=30)


async def count_for(http: httpx.AsyncClient, *, chapter: str, court: str | None) -> int:
    """Return the total count (across all pages) for one filter combo."""
    params = {
        "type": "r",
        "q": f"chapter:{chapter}",
        "filed_after": WINDOW_START.isoformat(),
        "filed_before": WINDOW_END.isoformat(),
        "page_size": 1,  # we only need `count`, not actual rows
    }
    if court is not None:
        params["court"] = court
    data = await _get(http, BASE, params)
    await asyncio.sleep(INTER_PAGE_SLEEP_S)
    return data.get("count", 0)


async def fetch_sample(
    http: httpx.AsyncClient, *, chapter: str, court: str, max_results: int = 50
) -> list[dict]:
    """Pull up to `max_results` results in the window for one (court, chapter).

    Capped at one page (page_size=50) to keep within rate limits — this is a
    diagnostic, not a full backfill.
    """
    params = {
        "type": "r",
        "court": court,
        "q": f"chapter:{chapter}",
        "filed_after": WINDOW_START.isoformat(),
        "filed_before": WINDOW_END.isoformat(),
        "page_size": max_results,
        "order_by": "dateFiled desc",
    }
    data = await _get(http, BASE, params)
    await asyncio.sleep(INTER_PAGE_SLEEP_S)
    return data.get("results", [])[:max_results]


async def volume_check(http: httpx.AsyncClient) -> None:
    print("=" * 70)
    print(f"VOLUME CHECK — window {WINDOW_START} .. {WINDOW_END}")
    print("=" * 70)
    for chapter in CHAPTERS:
        nationwide = await count_for(http, chapter=chapter, court=None)
        per_court = {}
        for c in OUR_COURTS:
            per_court[c] = await count_for(http, chapter=chapter, court=c)
        ours_total = sum(per_court.values())
        share = (ours_total / nationwide * 100.0) if nationwide else 0.0
        print()
        print(f"Chapter {chapter}:")
        print(f"  nationwide: {nationwide:>5}")
        for c in OUR_COURTS:
            print(f"  {c:<6}     {per_court[c]:>5}")
        print(f"  4-court sum:{ours_total:>5}   ({share:.1f}% of nationwide)")


async def quality_check(http: httpx.AsyncClient) -> None:
    print()
    print("=" * 70)
    print("QUALITY CHECK — classification mix in sample 'small' districts")
    print("=" * 70)
    for court in SAMPLE_COURTS:
        for chapter in CHAPTERS:
            results = await fetch_sample(http, chapter=chapter, court=court)
            tally: Counter[str] = Counter()
            errors = 0
            for r in results:
                try:
                    event, _debtors = normalize_courtlistener_result(r)
                    tally[event.debtor_classification] += 1
                except Exception:
                    errors += 1
            total = sum(tally.values())
            print()
            print(f"{court} chapter {chapter} (n={total}, errors={errors}):")
            for cls in ["business", "individual", "unknown"]:
                n = tally.get(cls, 0)
                pct = (n / total * 100.0) if total else 0.0
                print(f"  {cls:<10} {n:>4}  ({pct:.0f}%)")


async def main() -> None:
    async with httpx.AsyncClient() as http:
        await volume_check(http)
        await quality_check(http)


if __name__ == "__main__":
    asyncio.run(main())
