"""CourtListener REST API v4 client.

Wraps the public search endpoint (`/api/rest/v4/search/?type=r`) used to
discover bankruptcy filings. Auth is via the Token header. Pagination uses
cursors returned in the `next` field of each response.

We retry on 429 / 5xx and on transient network errors. We do NOT retry on
4xx auth errors (they won't fix themselves on retry).
"""

import asyncio
import logging
import os
from collections.abc import AsyncIterator
from typing import Any, Optional

import httpx
from tenacity import (
    AsyncRetrying,
    retry_if_exception,
    stop_after_attempt,
)

logger = logging.getLogger(__name__)

# CourtListener's documented authenticated quota is 5 req/min, 50/hour, 125/day.
# Sleep this long between pages of one search so we stay under the per-minute
# ceiling without burning retries. See DECISIONS §1.6 for the math.
#
# Overridable via env var `CL_INTER_PAGE_SLEEP_S` so long-running backfills
# (nationwide Ch 7 = ~700 pages) can dial up the pacing without code changes
# if the hourly limit starts biting.
INTER_PAGE_SLEEP_S = float(os.environ.get("CL_INTER_PAGE_SLEEP_S", "13.0"))


def is_retryable(exc: BaseException) -> bool:
    """True for HTTP statuses worth retrying (429 + 5xx) and transient
    network/timeout errors. Exposed as part of the module's public API so
    diagnostic scripts can share the same retry semantics."""
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in {429, 500, 502, 503, 504}
    return isinstance(exc, (httpx.TimeoutException, httpx.NetworkError))


def _exc_label(exc: BaseException) -> str:
    """Short tag for the retry log line."""
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}"
    return type(exc).__name__


def _backoff_wait_seconds(exc: BaseException, attempt: int) -> float:
    """Compute how long to wait before the next retry.

    Three rules in priority order:
      1. If the server sent `Retry-After`, honor it (RFC-compliant).
      2. For 429s, use a longer initial backoff (60s) so the per-minute
         window fully clears. Capped at 300s — past that we're probably
         hitting an hourly limit that needs the whole window to roll.
      3. For transient 5xx/network errors, a shorter exponential is fine.
    """
    # 1. Server-directed wait
    if isinstance(exc, httpx.HTTPStatusError):
        retry_after = exc.response.headers.get("Retry-After")
        if retry_after:
            try:
                return float(retry_after)
            except ValueError:
                pass  # fall through to heuristics

    # 2. 429: 60s, 120s, 240s, 300, 300, 300, 300, 300, 300, 300  (cumulative ~33min)
    is_429 = (
        isinstance(exc, httpx.HTTPStatusError)
        and exc.response.status_code == 429
    )
    if is_429:
        return min(60 * (2 ** (attempt - 1)), 300)

    # 3. Other transient errors: 5s, 10s, 20s, ...
    return min(5 * (2 ** (attempt - 1)), 60)


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

    async def _request(self, url: str, params: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        """GET with retry. Uses a hand-rolled wait function so we can honor
        the server's `Retry-After` header (which exponential alone can't see)
        and apply 429-specific backoff that starts at 60s.

        10 attempts total. With a worst-case 60+120+240+300+...+300 wait
        chain, that's ~33 minutes max before we give up — enough to ride
        out the documented 50/hour cap if it kicks in mid-run.
        """
        headers = {"Authorization": f"Token {self._token}"}
        max_attempts = 10
        async for attempt_ctx in AsyncRetrying(
            retry=retry_if_exception(is_retryable),
            stop=stop_after_attempt(max_attempts),
            reraise=True,
            wait=lambda rs: _backoff_wait_seconds(
                rs.outcome.exception(), rs.attempt_number
            ),
            before_sleep=lambda rs: logger.warning(
                "CL %s on attempt %d/%d; waiting %.0fs before retry",
                _exc_label(rs.outcome.exception()),
                rs.attempt_number,
                max_attempts,
                _backoff_wait_seconds(rs.outcome.exception(), rs.attempt_number),
            ),
        ):
            with attempt_ctx:
                response = await self._http.get(url, params=params, headers=headers)
                response.raise_for_status()
                return response.json()
        # Unreachable — AsyncRetrying either returns or raises
        raise RuntimeError("retry loop fell through without returning")

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
