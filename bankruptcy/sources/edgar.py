"""SEC EDGAR full-text search client.

Queries the public EFTS API (https://efts.sec.gov/LATEST/search-index) for
8-K filings whose body discloses Item 1.03 (Bankruptcy or Receivership). No
auth required — SEC just asks for a User-Agent header identifying the
requester per their fair-access policy.

Why EDGAR alongside CourtListener:
- Faster: public companies must file an 8-K within 4 business days of
  filing for bankruptcy, so EDGAR can beat CourtListener (which depends on
  PACER ingestion that may lag a day or so).
- Different debtor coverage: only public companies, but every public
  company that files for bankruptcy ends up here.
- Cross-validation: when an EDGAR record matches a CourtListener docket,
  we can boost classification confidence to 1.0 (a public company is by
  definition a business entity).
"""

from collections.abc import AsyncIterator
from datetime import date
from typing import Any, Optional

import httpx
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in {429, 500, 502, 503, 504}
    return isinstance(exc, (httpx.TimeoutException, httpx.NetworkError))


class EdgarClient:
    """Async client for SEC EDGAR full-text search + filing body fetch."""

    BASE_URL = "https://efts.sec.gov/LATEST/search-index"
    ARCHIVES_BASE = "https://www.sec.gov/Archives/edgar/data"
    DEFAULT_USER_AGENT = "Bankruptcy Pilot ernstfranciscerik@gmail.com"

    def __init__(
        self,
        user_agent: str = DEFAULT_USER_AGENT,
        http: Optional[httpx.AsyncClient] = None,
    ):
        self._user_agent = user_agent
        self._http = http or httpx.AsyncClient(timeout=30.0)
        self._owns_http = http is None

    async def __aenter__(self) -> "EdgarClient":
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        if self._owns_http:
            await self._http.aclose()

    @retry(
        retry=retry_if_exception(_is_retryable),
        wait=wait_exponential(multiplier=1, min=1, max=30),
        stop=stop_after_attempt(5),
        reraise=True,
    )
    async def _request(self, params: dict[str, Any]) -> dict[str, Any]:
        headers = {"User-Agent": self._user_agent, "Accept": "application/json"}
        response = await self._http.get(self.BASE_URL, params=params, headers=headers)
        response.raise_for_status()
        return response.json()

    @retry(
        retry=retry_if_exception(_is_retryable),
        wait=wait_exponential(multiplier=1, min=1, max=30),
        stop=stop_after_attempt(5),
        reraise=True,
    )
    async def fetch_filing_body(
        self,
        accession: str,
        primary_doc: str,
        cik: str,
    ) -> Optional[str]:
        """Fetch the raw HTML body of an 8-K filing.

        EDGAR's archive URL is `{ARCHIVES_BASE}/{cik_no_leading_zeros}/{accession_no_dashes}/{primary_doc}`.
        Returns the response text, or None on a 404 (filing was withdrawn
        or never indexed). 4xx other than 404 raises — that's a bug in our
        URL construction, not a missing filing.

        Respects SEC's fair-access policy (User-Agent header, low req rate —
        callers should not parallelize aggressively; the daily ingest rate
        is well under the 10 req/sec cap).
        """
        accession_no_dashes = accession.replace("-", "")
        cik_no_leading = cik.lstrip("0") or "0"
        url = f"{self.ARCHIVES_BASE}/{cik_no_leading}/{accession_no_dashes}/{primary_doc}"
        headers = {"User-Agent": self._user_agent}
        response = await self._http.get(url, headers=headers)
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.text

    async def search_bankruptcy_8k(
        self,
        *,
        start_date: date,
        end_date: date,
        max_results: int = 1000,
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield 8-K filings whose body mentions Item 1.03 within [start, end].

        Filters server-side via the full-text query and date range. Filters
        client-side to confirm '1.03' actually appears in the structured
        `items` list — full-text search can return false matches where
        "Item 1.03" appears in narrative context rather than as a section
        header.
        """
        page_size = 100
        offset = 0
        yielded = 0
        while yielded < max_results:
            params = {
                "q": '"Item 1.03"',
                "forms": "8-K",
                "dateRange": "custom",
                "startdt": start_date.isoformat(),
                "enddt": end_date.isoformat(),
                "from": offset,
            }
            data = await self._request(params)
            hits = data.get("hits", {}).get("hits", [])
            if not hits:
                return
            for hit in hits:
                if yielded >= max_results:
                    return
                source = hit.get("_source", {})
                if "1.03" not in (source.get("items") or []):
                    continue
                yield {
                    **source,
                    "_id": hit.get("_id"),
                    "_score": hit.get("_score"),
                }
                yielded += 1
            if len(hits) < page_size:
                return
            offset += page_size
