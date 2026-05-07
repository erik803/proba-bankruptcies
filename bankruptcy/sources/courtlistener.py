"""CourtListener REST API v4 client.

Wraps the public search endpoint (`/api/rest/v4/search/?type=r`) used to
discover bankruptcy filings. Auth is via the Token header. Pagination uses
cursors returned in the `next` field of each response.

We retry on 429 / 5xx and on transient network errors. We do NOT retry on
4xx auth errors (they won't fix themselves on retry).
"""

from collections.abc import AsyncIterator
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
        retry=retry_if_exception(_is_retryable),
        wait=wait_exponential(multiplier=1, min=1, max=30),
        stop=stop_after_attempt(5),
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
        court: str,
        query: str,
        page_size: int = 50,
        order_by: str = "dateFiled desc",
        max_results: int = 1000,
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield individual results from the RECAP search, walking cursor pagination.

        Stops when the server has no more pages OR when `max_results` results
        have been yielded. The `next` URL embeds the cursor and original
        filters, so we pass `params=None` for follow-up requests.
        """
        url: Optional[str] = f"{self.BASE_URL}/search/"
        params: Optional[dict[str, Any]] = {
            "type": "r",
            "court": court,
            "q": query,
            "order_by": order_by,
            "page_size": page_size,
        }
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
