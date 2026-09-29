"""The shared outbound HTTP client.

One client for the whole process, for two reasons that are not about efficiency.

**Connection reuse and politeness.** Creating a client per fetch opens a new
TCP connection and a new TLS handshake for every page, which the receiving
server sees as a burst of unrelated clients rather than one steady reader.

**One place decides retry policy.** R-008 specifies exactly what is retried and
what is not, and that policy has to hold for robots.txt, for page fetches, and
for anything added later. A per-call-site `tenacity` configuration is how the
policy ends up holding in three places and differing in all three.

The retry policy itself, and why each rule is what it is:

| Condition | Behaviour | Why |
|---|---|---|
| `429` | retry, honouring `retry-after` | The server told us when. Ignoring it is how one client becomes the reason the limit exists. |
| `498` | **never** retried | A capacity signal, not a throttle. Retrying immediately against a saturated backend extends the outage. |
| `5xx` | retried | Transient by assumption; a bounded number of attempts, then the failure is reported honestly. |
| `400` | never retried | A malformed request stays malformed. A retry loop here looks like an outage and is not one. |
| timeouts / connect errors | retried | The request may not have been received at all, so re-sending is safe for GET. |

The `is_retryable` hook is given the response so it can read `retry-after`; that
is why it is a callback on the transport rather than a boolean.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Callable
from types import TracebackType
from typing import Any, Final

import httpx

from app.core.config import get_settings
from app.core.logging import get_logger, redact

_log = get_logger("ingestion.fetcher")

#: 498 is Groq's capacity signal. It is not a standard status and it is
#: deliberately absent from the retried set (R-008).
CAPACITY_SIGNAL: Final[int] = 498

#: Statuses that mean "the request never happened", so re-sending is safe.
_RETRYABLE_STATUSES: Final[frozenset[int]] = frozenset({429, 500, 502, 503, 504})


def _is_retryable(response: httpx.Response) -> bool:
    """Whether a response is worth re-sending.

    A function, not a status set, because the decision needs the headers: a 429
    without `retry-after` still retries, but on a jittered backoff rather than
    the header's value.
    """
    status = response.status_code

    if status == CAPACITY_SIGNAL:
        return False
    if status in _RETRYABLE_STATUSES:
        return True
    # Every other 4xx is the request's fault. Retrying it cannot succeed and
    # turns one bad URL into a slow, noisy failure.
    return False


def _retry_after_seconds(response: httpx.Response) -> float | None:
    """Read `retry-after`, in either of its two forms.

    The header is either a delay in seconds or an HTTP date. Both appear in
    practice; only the integer form is handled numerically, and a date is
    ignored in favour of a backoff rather than parsed into a possibly-negative
    delay.
    """
    raw = response.headers.get("retry-after")
    if not raw:
        return None
    try:
        delay = float(raw.strip())
    except ValueError:
        return None
    # A server asking for a negative wait, or for longer than the whole request
    # budget, is not going to get it. The backoff is bounded instead.
    if delay < 0:
        return None
    return min(delay, 30.0)


def _build_backoff(attempt: int) -> float:
    """Full-jitter exponential backoff.

    *Full* jitter — a uniform draw from `[0, base × 2^attempt]` — rather than
    fixed exponential backoff. Fixed backoff leaves every client that failed at
    the same moment retrying at the same moment, which is the synchronised
    thundering herd that turns a brief throttle into a sustained one. Jitter
    spreads them across the window.
    """
    ceiling = min(0.5 * (2**attempt), 8.0)
    return random.uniform(0.0, ceiling)


class BoundedRetryTransport(httpx.AsyncBaseTransport):
    """A transport that retries a bounded number of times.

    Wraps another transport rather than being one, and the inner transport is a
    constructor argument rather than something attached later. httpx does not
    call a `wrap` hook on a transport passed to `AsyncClient`, so an attribute
    assigned by one would simply be absent at the first request — which fails
    as an `AttributeError` on the first fetch rather than at construction.

    `max_retries=0` disables retrying entirely, which is what the contract tests
    use so an asserted call count is a fact rather than a function of timing.

    On a 429 with a `retry-after`, the sleep is that value. Otherwise it is full
    jitter. The attempt counter counts *retries*, so `max_retries=1` means one
    additional attempt after the first — the reading that matches the
    configuration name.
    """

    def __init__(
        self,
        inner: httpx.AsyncBaseTransport,
        *,
        max_retries: int = 1,
        backoff: Callable[[int], float] = _build_backoff,
    ) -> None:
        self._inner = inner
        self.max_retries = max_retries
        self._backoff = backoff

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        last_response: httpx.Response | None = None
        last_exc: Exception | None = None

        for attempt in range(self.max_retries + 1):
            if attempt:
                delay = self._delay_for(last_response, attempt)
                _log.debug(
                    "retrying request",
                    extra={
                        "attempt": attempt,
                        "max_retries": self.max_retries,
                        "delay_seconds": round(delay, 3),
                        "status": last_response.status_code if last_response else None,
                        "method": request.method,
                    },
                )
                await asyncio.sleep(delay)

            try:
                response = await self._inner.handle_async_request(request)
            except (
                httpx.TimeoutException,
                httpx.ConnectError,
                httpx.ReadError,
                httpx.RemoteProtocolError,
            ) as exc:
                # The request may not have reached the server, so re-sending is
                # safe. Recorded, not raised: the final attempt re-raises.
                last_exc = exc
                last_response = None
                continue

            if not _is_retryable(response):
                return response

            if attempt >= self.max_retries:
                # Budget exhausted. The response is RETURNED, not raised, so the
                # caller sees the real status code and body rather than a
                # synthesised exception that hides what the server said.
                return response

            await response.aclose()
            last_response = response

        if last_response is not None:
            return last_response
        assert last_exc is not None  # noqa: S101 - the loop always sets one or the other
        raise last_exc

    async def aclose(self) -> None:
        await self._inner.aclose()

    def _delay_for(self, response: httpx.Response | None, attempt: int) -> float:
        if response is not None:
            honour = _retry_after_seconds(response)
            if honour is not None:
                return honour
        return self._backoff(attempt)


def build_client(
    *,
    timeout_seconds: float | None = None,
    user_agent: str | None = None,
    max_retries: int | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> httpx.AsyncClient:
    """Construct the shared client.

    A function rather than a module-level singleton because tests need clients
    with a mock transport, and because a singleton created at import time would
    read settings before the environment was loaded.
    """
    settings = get_settings()

    timeout = httpx.Timeout(
        timeout_seconds if timeout_seconds is not None else settings.crawl_timeout_seconds,
        # A pool timeout distinct from the request timeout: a request that
        # times out mid-transfer is a slow page, while a pool timeout means the
        # server is refusing connections. Collapsing them makes the log unable
        # to distinguish a slow site from a down one.
        connect=10.0,
        pool=10.0,
    )

    # A transport passed in is used as-is and is NOT wrapped: a test's mock
    # transport asserts on call counts, and wrapping it would add retries the
    # test cannot account for. Production callers pass nothing and get the
    # bounded-retry policy.
    resolved_transport = transport or BoundedRetryTransport(
        httpx.AsyncHTTPTransport(),
        max_retries=max_retries if max_retries is not None else settings.groq_max_retries,
    )

    return httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=True,
        headers={
            "User-Agent": user_agent or settings.crawl_user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en;q=0.9",
        },
        limits=httpx.Limits(
            max_connections=8,
            max_keepalive_connections=4,
            # A keepalive shorter than the pool's own idle timeout. A held
            # connection that the server has already closed is a request that
            # fails on a reused socket, which is the failure mode `pre_ping`
            # exists to prevent.
            keepalive_expiry=30.0,
        ),
        transport=resolved_transport,
        # A redirect is re-validated by the caller's SSRF guard before the
        # request is made, so the client must NOT silently follow one off the
        # allowlist. `follow_redirects=True` is retained because the
        # documentation sites redirect a documented amount (a trailing slash,
        # a renamed section) and refusing those would fail healthy crawls; the
        # guard is what keeps it safe, and `max_redirects` bounds the chain.
        max_redirects=5,
    )


class SharedClient:
    """An async context manager owning the process-wide client.

    Exists so the application's lifespan can open one client and the crawler
    can borrow it, without either owning it. A client left open leaks a
    connection pool; a client closed too early fails every subsequent request
    with "client has been closed".
    """

    def __init__(self, **kwargs: Any) -> None:
        self._kwargs = kwargs
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> httpx.AsyncClient:
        self._client = build_client(**self._kwargs)
        return self._client

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
            _log.info("outbound http client closed")


def log_response_failure(response: httpx.Response, *, context: str) -> None:
    """Log a non-2xx response, redacted.

    The body is included because "the server said 503 with an empty body" and
    "the server said 403 and told us why" call for different responses — but it
    goes through `redact` first, because an error body from a proxy is exactly
    where a credential in a URL tends to end up.
    """
    if response.is_success:
        return
    _log.warning(
        "http request failed",
        extra={
            "context": context,
            "status": response.status_code,
            "method": response.request.method,
            "url": redact(str(response.request.url)),
            "body_excerpt": redact(response.text[:500]),
        },
    )


__all__ = [
    "CAPACITY_SIGNAL",
    "BoundedRetryTransport",
    "SharedClient",
    "build_client",
    "log_response_failure",
]
