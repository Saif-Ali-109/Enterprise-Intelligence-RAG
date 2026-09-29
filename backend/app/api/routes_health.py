"""`GET /health` — liveness and per-dependency readiness.

The contract requires the three dependencies to report **independently**
(`database`, `vector_store`, `language_model`). That is the whole point of the
endpoint: a rolled-up `status` alone would present a Pinecone outage as "the
service is unhealthy" and leave the operator guessing which vendor to look at.

Two rules the probes obey, and they are the reason this is a module rather than
three calls in a route handler:

**A probe never raises.** A dependency that is down is the normal case this
endpoint exists to report, so an exception from a probe is caught and turned
into a status. An endpoint that 500s when the thing it monitors is broken is
worse than no endpoint: it is unavailable exactly when it is needed.

**A probe never reports internals.** `detail` is an authored phrase. It is not a
driver message, a connection string, a provider error body, or a stack trace
(FR-047). A health endpoint is polled by monitoring systems on a schedule, often
without anyone reading the logs, which makes it one of the least protected
endpoints an application has.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable

from fastapi import APIRouter, Depends, Request

from app.core.ratelimit import enforce_general
from app.core.sanitize import sanitise_text
from app.schemas import DependencyReport, DependencyStatus, HealthResponse

router = APIRouter(tags=["system"])


# ============================================================================
# Probe registry
# ============================================================================

#: A probe returns `None` on success, or an authored reason string for the
#: failure. Returning a string rather than raising keeps the "what went wrong"
#: decision in the adapter that knows the vendor, and keeps it away from the
#: endpoint.
ProbeFn = Callable[[], Awaitable[str | None]]

#: Dependency keys, matching the contract's required property names exactly.
DATABASE: str = "database"
VECTOR_STORE: str = "vector_store"
LANGUAGE_MODEL: str = "language_model"

#: The order they are reported in. A dict literal preserves insertion order and
#: the response is a dict, so this is the order a client sees.
DEPENDENCY_ORDER: tuple[str, ...] = (DATABASE, VECTOR_STORE, LANGUAGE_MODEL)

_probes: dict[str, ProbeFn] = {}


def register_probe(dependency: str, probe: ProbeFn) -> None:
    """Register the probe for a dependency.

    Called by the application factory once the adapters exist, and by the test
    suite with fakes. A dependency with no registered probe is reported
    `unavailable` with an explicit detail rather than being omitted: an
    omitted key would fail the contract's `required` list, and a defaulted
    `ok` would be a lie.
    """
    _probes[dependency] = probe


def reset_probes() -> None:
    """Drop every registered probe. Test teardown, and application shutdown."""
    _probes.clear()


def registered_probes() -> dict[str, ProbeFn]:
    """The current probe map. A copy, so a caller cannot mutate the registry."""
    return dict(_probes)


async def _run_probe(dependency: str) -> DependencyReport:
    """Run one probe and turn its outcome into a report."""
    probe = _probes.get(dependency)

    if probe is None:
        return DependencyReport(
            status=DependencyStatus.UNAVAILABLE,
            detail="No probe is registered for this dependency.",
            latency_ms=None,
        )

    started = time.perf_counter()
    try:
        reason = await probe()
    except Exception as exc:  # noqa: BLE001 - a probe must not break /health
        # The exception TYPE is enough to act on and cannot contain a
        # credential. The message is redacted anyway, because a vendor SDK
        # exception routinely carries the request that caused it.
        reason = f"Probe raised {type(exc).__name__}."

    latency_ms = int(round((time.perf_counter() - started) * 1000))

    if reason is None:
        return DependencyReport(status=DependencyStatus.OK, detail=None, latency_ms=latency_ms)

    return DependencyReport(
        status=DependencyStatus.UNAVAILABLE,
        detail=sanitise_text(reason)[:200],
        latency_ms=latency_ms,
    )


def _overall(reports: dict[str, DependencyReport]) -> DependencyStatus:
    """Roll the three reports up into one status.

    `unavailable` if any dependency is unavailable — the service genuinely
    cannot answer a question in that state. `degraded` is reserved for a
    dependency that is up but impaired, which is a state a probe reports by
    returning a reason at `degraded` severity rather than as an exception.

    Liveness and readiness are not conflated: this is readiness. A process that
    is running but cannot reach its vector store is not ready, and reporting
    `ok` here would let a load balancer keep sending it traffic.
    """
    statuses = {report.status for report in reports.values()}
    if DependencyStatus.UNAVAILABLE in statuses:
        return DependencyStatus.UNAVAILABLE
    if DependencyStatus.DEGRADED in statuses:
        return DependencyStatus.DEGRADED
    return DependencyStatus.OK


# ============================================================================
# The database probe
# ============================================================================


async def database_probe() -> str | None:
    """`SELECT 1` against the application's own pool.

    Uses the real pool rather than opening a fresh connection: what matters is
    whether the *application* can reach the database, and a fresh connection
    would pass while the pool is exhausted — which is precisely when the
    service is broken and a green health check says otherwise.
    """
    from sqlalchemy import text

    from app.db.session import get_engine

    try:
        async with get_engine().connect() as connection:
            await connection.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        return f"Database unreachable ({type(exc).__name__})."
    return None


# ============================================================================
# Route
# ============================================================================


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Liveness and dependency readiness",
    description=(
        "Reports `database`, `vector_store`, and `language_model` independently so a "
        "dependency outage is visible as that dependency's outage rather than as a generic "
        "failure. Contains no secret values and no internal detail (FR-042, FR-047)."
    ),
    # `health` is unauthenticated in the contract and is polled by Compose and
    # by monitoring, so it must answer even when the rest of the API is
    # degraded. The rate limit still applies: an unbounded health endpoint is a
    # way to make the service spend work without asking it a question.
    dependencies=[Depends(enforce_general)],
)
async def get_health(request: Request) -> HealthResponse:
    """Readiness, rolled up per dependency.

    Probes run concurrently: readiness should not take the sum of three vendor
    timeouts, and an operator watching a dashboard needs the answer now rather
    than in three timeouts' time.
    """
    results = await asyncio.gather(*(_run_probe(name) for name in DEPENDENCY_ORDER))
    reports = dict(zip(DEPENDENCY_ORDER, results, strict=True))

    from app import __version__

    return HealthResponse(
        status=_overall(reports),
        version=__version__,
        dependencies=reports,
    )


__all__ = [
    "DATABASE",
    "DEPENDENCY_ORDER",
    "LANGUAGE_MODEL",
    "VECTOR_STORE",
    "database_probe",
    "get_health",
    "register_probe",
    "registered_probes",
    "reset_probes",
    "router",
]
