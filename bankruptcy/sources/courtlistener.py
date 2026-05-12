"""CourtListener REST API v4 client.

Wraps the public search endpoint (`/api/rest/v4/search/?type=r`) used to
discover bankruptcy filings. Auth is via the Token header. Pagination uses
cursors returned in the `next` field of each response.

We retry on 429 / 5xx and on transient network errors. We do NOT retry on
4xx auth errors (they won't fix themselves on retry).
"""

import asyncio
from collections.abc import AsyncIterator
from typing import Any, Optional

import httpx
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

# CourtListener's documented authenticated quota is 5 req/min, 50/hour, 125/day.
# Sleep this long between pages of one search so we stay under the per-minute
# ceiling without burning retries. See DECISIONS §1.6 for the math.
INTER_PAGE_SLEEP_S = 13.0


def is_retryable(exc: BaseException) -> bool:
    """True for HTTP statuses worth retrying (429 + 5xx) and transient
    network/timeout errors. Exposed as part of the module's public API so
    diagnostic scripts can share the same retry semantics."""
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in {429, 500, 502, 503, 504}
    return isinstance(exc, (httpx.TimeoutException, httpx.NetworkError))


class CourtListenerClient:
    """Thin async client over CourtListener's RECAP search endpoint."""

    BASE_URL = "https://www.courtlistener.com/api/rest/v4"

    def __init__(self, token: str, http: Optional[httpx.AsyncClient] = None):
        self._token = token
        self._http = http or httpx.AsyncClient(timeout=30.0)
        self._owns_http = http is None

    async def __aenter__(self) -> "CourtListenerClient":
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        if self._owns_http:
            await self._http.aclose()

    @retry(
        retry=retry_if_exception(is_retryable),
        # min=20s so a single 429 wait clears the per-minute window;
        # max=120s as ceiling; up to 8 attempts so total budget covers
        # the full 60s rate-limit window plus jitter.
        wait=wait_exponential(multiplier=2, min=20, max=120),
        stop=stop_after_attempt(8),
        reraise=True,
    )
    async def _request(self, url: str, params: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        headers = {"Authorization": f"Token {self._token}"}
        response = await self._http.get(url, params=params, headers=headers)
        response.raise_for_status()
        return response.json()

    async def search_recap(
        self,
        *,
        court: Optional[str] = None,
        query: str,
        filed_after: Optional[str] = None,
        filed_before: Optional[str] = None,
        page_size: int = 50,
        order_by: str = "dateFiled desc",
        max_results: int = 1000,
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield individual results from the RECAP search, walking cursor pagination.

        When `court` is None, the search runs nationwide — useful for steady-state
        polling where one request scans all 95 courts at once (see DECISIONS §1.6
        on rate-limit math).

        `filed_after` / `filed_before` are ISO dates (YYYY-MM-DD) the API uses
        to bound the result set. Watermark-style polling sets `filed_after` to
        the most recent event we've already ingested.

        Stops when the server has no more pages OR when `max_results` results
        have been yielded. The `next` URL embeds the cursor and original
        filters, so we pass `params=None` for follow-up requests.
        """
        url: Optional[str] = f"{self.BASE_URL}/search/"
        params: Optional[dict[str, Any]] = {
            "type": "r",
            "q": query,
            "order_by": order_by,
            "page_size": page_size,
        }
        if court is not None:
            params["court"] = court
        if filed_after is not None:
            params["filed_after"] = filed_after
        if filed_before is not None:
            params["filed_before"] = filed_before
        yielded = 0
        while url is not None and yielded < max_results:
            data = await self._request(url, params=params)
            for result in data.get("results", []):
                if yielded >= max_results:
                    return
                yield result
                yielded += 1
            url = data.get("next")
            params = None
            # Pace the next page to stay under the 5/min CL limit. No-op on
            # the last page (loop exits before sleeping).
            if url is not None and yielded < max_results:
                await asyncio.sleep(INTER_PAGE_SLEEP_S)
