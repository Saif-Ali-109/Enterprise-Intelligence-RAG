"""Inbound rate limiting.

FR-045. A token bucket per client address, evaluated per route class.

Two decisions worth stating:

**Limiting rejects; it does not degrade.** A `429` is the response. There is no
slower path, no smaller candidate pool, no cached answer served in place of a
live one. A rate limiter that makes the service *worse* under load rather than
telling the caller to back off has not solved the problem it was added for, and
it has done so in the least visible way available — the caller sees a wrong
answer and blames the ranking.

**It is in-process.** An in-memory bucket is per worker, so a multi-worker
deployment gets N times the configured rate. For a single-operator local tool
that is the correct trade: it is one process, it needs no Redis, and a shared
store would be a network dependency on the hot path of every request for a
limit that is about protecting a laptop. `RATE_LIMIT_*` are recorded on
`/config` so the effective limit is visible rather than assumed.

The clock is injectable, so a test can advance time instead of sleeping. A
limiter tested by sleeping is a limiter whose tests take seconds and fail on a
loaded machine.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field

# Imported at runtime, NOT under TYPE_CHECKING, despite this module using
# `from __future__ import annotations`. FastAPI resolves a dependency's
# annotations with `typing.get_type_hints`, and an annotation whose name is only
# imported for type checking does not resolve — FastAPI then falls back to
# treating the parameter as a query parameter, so a request silently becomes a
# 400 "Field required: query" and the dependency never runs. The rate limit
# stops being enforced and nothing says why.
#
# The same applies to every symbol FastAPI introspects in this project: it is a
# module-level rule, not a one-off import.
from fastapi import Request

from app.core.config import get_settings
from app.core.errors import RateLimited
from app.core.logging import get_logger

_log = get_logger("core.ratelimit")

#: The class of limit applied to a route. Two classes, not a per-route table:
#: a per-route configuration is a table nobody maintains, and the actual
#: distinction that matters is "may this cost money" — asking a question calls
#: two model providers, and browsing a source list calls none.
RouteClass = str

GENERAL: RouteClass = "general"
ASK: RouteClass = "ask"


@dataclass
class _Bucket:
    """One client's allowance.

    Tokens refill continuously at `rate` per second rather than resetting on a
    fixed window. A fixed window lets a client spend the whole minute's budget
    in the last second and the whole next budget in the first second of the next
    minute, which is a 2× burst across a boundary — the classic fixed-window
    problem, and a real one for a rate limit whose purpose is to smooth load.
    """

    tokens: float
    updated_at: float
    capacity: float
    rate_per_second: float

    def consume(self, *, now: float, cost: float = 1.0) -> tuple[bool, float]:
        """Spend `cost` tokens. Returns `(allowed, seconds_until_available)`.

        The refill is computed from elapsed time rather than accumulated in
        ticks, so a process that was busy for thirty seconds does not have to
        replay thirty seconds of refill.
        """
        elapsed = max(0.0, now - self.updated_at)
        self.tokens = min(self.capacity, self.tokens + elapsed * self.rate_per_second)
        self.updated_at = now

        if self.tokens >= cost:
            self.tokens -= cost
            return True, 0.0

        # The wait is for ONE token, not for the full cost. A multi-cost request
        # is refused and retried rather than admitted late, so the reported
        # delay is the earliest moment a retry could plausibly succeed.
        deficit = cost - self.tokens
        return False, deficit / self.rate_per_second


@dataclass
class RateLimiter:
    """Token-bucket limiter over a mapping of client key to bucket."""

    per_minute: int
    clock: Callable[[], float] = time.monotonic
    # Buckets are removed once they have been full for this long, so a client
    # that made one request an hour ago does not occupy an entry forever. An
    # unbounded map is a slow memory leak that looks like nothing at all.
    idle_expiry_seconds: float = 300.0
    _buckets: dict[str, _Bucket] = field(default_factory=dict)

    @property
    def capacity(self) -> float:
        return float(self.per_minute)

    @property
    def rate_per_second(self) -> float:
        return self.per_minute / 60.0

    def check(self, key: str) -> None:
        """Consume one token for `key`, or raise `RateLimited`.

        Raises rather than returning a verdict, so a call site cannot forget to
        act on the result. A `if not limiter.check(): ...` idiom is a limiter
        that gets skipped on the path where it mattered.
        """
        now = self.clock()
        bucket = self._buckets.get(key)

        if bucket is None:
            bucket = _Bucket(
                tokens=self.capacity,
                updated_at=now,
                capacity=self.capacity,
                rate_per_second=self.rate_per_second,
            )
            self._buckets[key] = bucket
        elif (
            now - bucket.updated_at > self.idle_expiry_seconds and bucket.tokens >= bucket.capacity
        ):
            # Idle and fully refilled. Reclaim the entry.
            del self._buckets[key]
            bucket = _Bucket(
                tokens=self.capacity,
                updated_at=now,
                capacity=self.capacity,
                rate_per_second=self.rate_per_second,
            )
            self._buckets[key] = bucket

        allowed, retry_after = bucket.consume(now=now)

        if not allowed:
            # Rounded UP: a `Retry-After` of 0 invites an immediate retry, which
            # is the opposite of what the header is for. The minimum of 1 is the
            # finest granularity an HTTP client acts on anyway.
            seconds = max(1, math.ceil(retry_after))
            _log.warning(
                "rate limit exceeded",
                extra={
                    "client": key,
                    "limit_per_minute": self.per_minute,
                    "retry_after_seconds": seconds,
                },
            )
            raise RateLimited(retry_after=seconds)

    def remaining(self, key: str) -> int:
        """Tokens left for `key`. For a response header; not a security control."""
        bucket = self._buckets.get(key)
        if bucket is None:
            return self.per_minute
        return int(bucket.tokens)

    def reset(self) -> None:
        """Drop all buckets. Test teardown."""
        self._buckets.clear()

    def __len__(self) -> int:
        return len(self._buckets)


# ============================================================================
# Process-wide limiters
# ============================================================================

_general_limiter: RateLimiter | None = None
_ask_limiter: RateLimiter | None = None


def get_general_limiter() -> RateLimiter:
    global _general_limiter
    if _general_limiter is None:
        _general_limiter = RateLimiter(per_minute=get_settings().rate_limit_general_per_minute)
    return _general_limiter


def get_ask_limiter() -> RateLimiter:
    global _ask_limiter
    if _ask_limiter is None:
        _ask_limiter = RateLimiter(per_minute=get_settings().rate_limit_ask_per_minute)
    return _ask_limiter


def reset_limiters() -> None:
    """Drop the process-wide limiters. Test teardown."""
    global _general_limiter, _ask_limiter
    _general_limiter = None
    _ask_limiter = None


# ============================================================================
# FastAPI dependency
# ============================================================================


def client_key(request: Request) -> str:
    """The bucket a request is charged against.

    Prefers the first entry of `Forwarded` / `X-Forwarded-For`, falling back to
    the socket peer. The socket peer alone would charge every request in a
    proxied deployment to one address, making the limit a global one — which
    means either one client can exhaust it for everyone, or the limit has to be
    set so high it protects nothing.

    The forwarded header is trusted, which is a real caveat: behind a proxy that
    lets a client set it, a client can pick its own bucket. That is the
    deployment's configuration to get right — and the alternative, using the
    peer address, is the failure mode above. The value is never logged with
    more than it already carries, and never used for anything but bucketing.
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        # The left-most entry is the original client; the rest are proxies we
        # added ourselves and would otherwise all collapse to one bucket.
        first = forwarded.split(",")[0].strip()
        if first:
            return first[:64]

    real_ip = request.headers.get("x-real-ip")
    if real_ip:
        return real_ip.strip()[:64]

    return (request.client.host if request.client else "unknown")[:64]


def enforce_general(request: Request) -> None:
    """FastAPI dependency applying the general limit."""
    get_general_limiter().check(client_key(request))


def enforce_ask(request: Request) -> None:
    """FastAPI dependency applying the question-submission limit.

    Separate from the general limit because a question costs two provider calls
    and a filter is served from PostgreSQL. One shared bucket would mean a
    client browsing the corpus could exhaust the allowance that stops a loop
    from spending money on generation.
    """
    get_ask_limiter().check(client_key(request))


__all__ = [
    "ASK",
    "GENERAL",
    "RateLimiter",
    "RouteClass",
    "client_key",
    "enforce_ask",
    "enforce_general",
    "get_ask_limiter",
    "get_general_limiter",
    "reset_limiters",
]
