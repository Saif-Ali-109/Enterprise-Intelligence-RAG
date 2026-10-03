"""Running a crawl for a registered source: the wiring between HTTP and registry.

`pipeline.run_crawl` knows how to take a list of candidate URLs, fetch them one
at a time, and record every outcome. It deliberately knows nothing about *where*
the candidates come from, which is what lets a test drive it with two URLs and a
mapping. This module is the other half: it turns a `sources` row into a
frontier, a robots gate, a fetch callable, and a vector store, and it runs the
crawl outside the request that asked for it.

Four decisions are here, and each is a place the obvious alternative is wrong:

**The crawl runs in an asyncio task, not a worker queue.** The constitution's
stack has no broker, and adding one to serve a single-operator tool would be a
second deployment to supervise for no benefit. The cost is honest and bounded:
a crawl dies with the process, which is why `reclaim_orphaned_jobs` exists and
why `cancel_all` marks in-flight jobs `cancelled` rather than leaving them
`running` — a job left `running` holds the partial unique index and would block
the source forever.

**One discovery policy for every source: sitemap, then links, as a union.** A
sitemap is the publisher's own statement of what exists, and following links is
what a site without one offers. Both are gathered and filtered once, then sorted
before the cap, so `max_pages` means the same pages on every run. Per-host
special cases were rejected: a host registered later would silently fall outside
a rule written for the six hosts that were measured.

**Robots decides before anything is fetched, using the same gate for discovery
and for the fetch stage.** One authorisation path, one cache, primed before use
(FR-028). An unprimed origin denies, which turns a wiring mistake into a refusal
rather than an unbounded crawl.

**The site's `Crawl-delay` is a floor, not a preference.** The effective delay is
the larger of the configured value and what `robots.txt` asks for. A crawler that
treats `Crawl-delay: 10` as advisory is a crawler that has decided the site's
wishes are optional.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.security import scope_prefix
from app.db.models import CrawlJob, Source
from app.db.session import get_session_factory
from app.ingestion.discovery import build_frontier
from app.ingestion.fetcher import SharedClient, fetch_page
from app.ingestion.pipeline import (
    CrawlResult,
    fail_crawl_job,
    run_crawl,
    start_crawl_job,
)
from app.ingestion.robots import RobotsGate
from app.retrieval.vector_store import VectorStore, get_vector_store

_log = get_logger("app.ingestion.crawl_service")

#: Where a sitemap is looked for. One conventional path, requested once.
#:
#: A sitemap *index* (`sitemap_index.xml`, or `/sitemap.xml` pointing at one) is
#: handled by discovery, which follows index children one level. Guessing at
#: further conventional paths would multiply requests for a guess, and a site
#: that wants to be indexed publishes a sitemap reference in `robots.txt` — which
#: is not read here because reading it for the *path* rather than the *rules*
#: would put the gate and the discoverer in a second authorisation path.
SITEMAP_PATH = "/sitemap.xml"


# ============================================================================
# In-flight crawls in this process
# ============================================================================

#: Source id -> the task crawling it.
#:
#: A module-level registry because "the crawls this process is running" is a fact
#: about the process, and threading a registry object through every route to reach
#: the same state would be worse. It is bookkeeping, not a guarantee: two workers
#: of one source are prevented by the partial unique index, not by this dict.
_TASKS: dict[uuid.UUID, asyncio.Task[None]] = {}


def in_flight() -> set[uuid.UUID]:
    """The sources this process is crawling right now."""
    return {source_id for source_id, task in _TASKS.items() if not task.done()}


def schedule_crawl(
    *,
    source_id: uuid.UUID,
    job: CrawlJob | None = None,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    client: httpx.AsyncClient | None = None,
    store: VectorStore | None = None,
) -> asyncio.Task[None]:
    """Start a crawl as a background task and return it.

    The task is tracked so shutdown can cancel it deliberately. A task nobody
    holds a reference to can still be collected mid-flight, and a crawl that
    stops halfway with no record of why is the exact failure FR-054 exists to
    prevent.

    `client` and `store` are injectable so a test can supply a fake transport and
    a recording store; production callers pass neither and get the shared client
    and the configured index.
    """
    existing = _TASKS.get(source_id)
    if existing is not None and not existing.done():
        _log.info("crawl already running in this process", extra={"source_id": str(source_id)})
        return existing

    factory = session_factory or get_session_factory()
    resolved_client = client
    task = asyncio.create_task(
        _execute(
            source_id=source_id,
            job=job,
            session_factory=factory,
            client=resolved_client,
            store=store,
        ),
        name=f"crawl-{source_id}",
    )
    _TASKS[source_id] = task
    task.add_done_callback(lambda done: _forget(source_id, done))
    return task


def _forget(source_id: uuid.UUID, task: asyncio.Task[None]) -> None:
    """Drop a finished task, logging a failure that escaped the crawl.

    The crawl records its own failures against the job; this callback exists for
    the ones that escaped the crawl entirely — a bug in the wiring, a cancelled
    task, an exception in the discovery step. Those are the ones that would
    otherwise leave a `running` job row and nothing else.
    """
    if _TASKS.get(source_id) is task:
        _TASKS.pop(source_id, None)
    if task.cancelled():
        _log.warning("crawl task cancelled", extra={"source_id": str(source_id)})
        return
    exc = task.exception()
    if exc is not None:
        _log.error(
            "crawl task failed",
            extra={"source_id": str(source_id), "error": type(exc).__name__},
        )


async def cancel_all(*, reason: str = "service shutting down") -> int:
    """Cancel every in-flight crawl and close its job as `cancelled`.

    Returns the number cancelled. A job row that is left `running` holds
    `crawl_jobs_one_active_per_source` and blocks the source's next crawl until
    someone edits the database by hand; `cancelled` is a terminal state that says
    what happened, and `reclaim_orphaned_jobs` cleans up the rows that outlive the
    process entirely.
    """
    tasks = [task for task in _TASKS.values() if not task.done()]
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    _TASKS.clear()

    factory = get_session_factory()
    async with factory() as session:
        result = await session.execute(
            select(CrawlJob).where(CrawlJob.status.in_(("queued", "running")))
        )
        jobs = list(result.scalars())
        for job in jobs:
            job.status = "cancelled"
            job.error_code = "INTERNAL_ERROR"
            job.error_message = f"The service stopped while this crawl was in progress ({reason})."
            job.finished_at = datetime.now(UTC)
        await session.commit()
    return len(tasks)


async def reclaim_orphaned_jobs(
    *,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    reason: str = "the service restarted",
) -> int:
    """Close jobs left `running` by a process that died. Returns how many.

    Without this, a crash mid-crawl leaves a row that claims to be running, with
    nothing running, holding the source's active-crawl slot. The only way out
    would be a manual database edit — and the operator who discovers that has no
    way to know it is a bug rather than a crawl that is genuinely slow.
    """
    factory = session_factory or get_session_factory()
    async with factory() as session:
        result = await session.execute(
            select(CrawlJob).where(CrawlJob.status.in_(("queued", "running")))
        )
        jobs = list(result.scalars())
        for job in jobs:
            job.status = "failed"
            job.error_code = "INTERNAL_ERROR"
            job.error_message = f"This crawl was interrupted: {reason}. Trigger it again."
            job.finished_at = datetime.now(UTC)
        await session.commit()
    if jobs:
        _log.warning(
            "reclaimed crawl jobs left running by a previous process",
            extra={"count": len(jobs)},
        )
    return len(jobs)


# ============================================================================
# Running one crawl
# ============================================================================


async def _execute(
    *,
    source_id: uuid.UUID,
    job: CrawlJob | None,
    session_factory: async_sessionmaker[AsyncSession],
    client: httpx.AsyncClient | None,
    store: VectorStore | None,
) -> None:
    """Own the session, the client, and the failure path for one crawl."""
    async with session_factory() as session:
        try:
            await crawl_source(
                session,
                source_id=source_id,
                client=client,
                store=store or get_vector_store(),
                job=job,
            )
        except asyncio.CancelledError:
            # The job row is closed by `cancel_all`, which knows the reason. A
            # rollback here undoes this crawl's own uncommitted work and nothing
            # else, because pages commit one at a time.
            await session.rollback()
            raise


async def crawl_source(
    session: AsyncSession,
    *,
    source_id: uuid.UUID,
    client: httpx.AsyncClient | None = None,
    store: VectorStore | None = None,
    job: CrawlJob | None = None,
    sleep: Callable[[float], Any] = asyncio.sleep,
) -> CrawlResult:
    """Discover, fetch, and index one source's pages.

    `client` and `store` default to the shared HTTP client and the configured
    index; a caller that owns neither (a test, an offline replay) passes both.
    `job` is the row the caller already created and committed for this crawl —
    the registration endpoint, whose response body carries it. One is created
    here when there is none, *before* the start page is fetched.
    """
    source = await session.get(Source, source_id)
    if source is None:
        from app.core.errors import ResourceNotFound

        raise ResourceNotFound(f"source {source_id} does not exist")

    # The row is read into plain values here because everything below commits.
    # A commit expires the instance, and reading an attribute off an expired
    # instance is database IO in a place that cannot await it: with
    # `pool_pre_ping` on, that surfaces as `MissingGreenlet` from the pool's
    # checkout rather than as the stale value it was aiming for.
    start_url = source.start_url
    allowed_domains = list(source.allowed_domains)

    settings = get_settings()
    page_cap = source.max_pages if source.max_pages is not None else settings.crawl_max_pages
    depth_cap = source.max_depth if source.max_depth is not None else settings.crawl_max_depth
    configured_delay = (
        source.delay_seconds if source.delay_seconds is not None else settings.crawl_delay_seconds
    )

    if job is None:
        # The job row exists before the first request, not part-way through the
        # run. `crawl_source` fetches the start page and reads the sitemap before
        # it builds a frontier, so a crawl that cannot reach its own start page
        # would otherwise leave *nothing* behind — and "a crawl was asked for and
        # nothing ran, and no job says so" is the one question an operator cannot
        # answer from the database.
        job = await start_crawl_job(session, source_id=source_id, target_url=start_url)
        await session.commit()

    owns_client = client is None
    owned = SharedClient()
    active_client = client if client is not None else await owned.__aenter__()
    store = store or get_vector_store()

    try:
        try:
            result = await _crawl_once(
                session,
                store=store,
                client=active_client,
                job=job,
                start_url=start_url,
                allowed_domains=allowed_domains,
                page_cap=page_cap,
                depth_cap=depth_cap,
                configured_delay=configured_delay,
                sleep=sleep,
            )
        except Exception as exc:  # noqa: BLE001 - recorded on the job, then re-raised
            # `run_crawl` closes the job for its own failures. This covers the
            # steps that happen before it starts, and re-closing a job it already
            # closed writes the same row.
            await fail_crawl_job(session, job_id=job.id, exc=exc)
            raise

        await _record_source_outcome(session, source_id, result)
        return result
    finally:
        if owns_client:
            await owned.__aexit__(None, None, None)


async def _crawl_once(
    session: AsyncSession,
    *,
    store: VectorStore,
    client: httpx.AsyncClient,
    job: CrawlJob,
    start_url: str,
    allowed_domains: list[str],
    page_cap: int,
    depth_cap: int,
    configured_delay: float,
    sleep: Callable[[float], Any],
) -> CrawlResult:
    """One pass: authorise, discover, then hand the pages to the orchestrator.

    Split out of `crawl_source` so the job bookkeeping around it reads as
    bookkeeping, and so every value here is a plain value rather than an ORM
    attribute that a commit will have expired by the time it is used.
    """
    gate = RobotsGate(client)
    # Primed before the frontier is built: `allows_sync` denies an origin it
    # has not read, so an unprimed gate would empty the frontier rather than
    # open the crawl.
    await gate.prime([start_url])

    start_response = await fetch_page(
        client,
        start_url,
        allowed_domains=allowed_domains,
        robots_gate=gate.allows_sync,
    )
    sitemap_xml = await _sitemap(client, start_url)

    delay = max(configured_delay, gate.crawl_delay(start_url) or 0.0)

    frontier = build_frontier(
        start_url,
        max_pages=page_cap,
        robots_allows=gate.allows_sync,
        sitemap_xml=sitemap_xml,
        link_html=start_response.text,
        max_depth=depth_cap,
    )

    async def fetch(url: str) -> Any:
        return await fetch_page(
            client,
            url,
            allowed_domains=allowed_domains,
            robots_gate=gate.allows_sync,
        )

    # `run_crawl` takes tuples and the frontier yields `Candidate` objects. The
    # projection is here rather than by widening `run_crawl` to accept both: one
    # candidate shape per function is a shape a reader can rely on, and a second
    # accepted shape is a second way to write a depth-0 entry with nothing to
    # notice which one a caller used.
    candidates = [(candidate.url, candidate.depth, candidate.via) for candidate in frontier.queued]
    return await run_crawl(
        session,
        store,
        source_id=job.source_id,
        fetch=fetch,
        candidates=candidates,
        max_pages=page_cap,
        max_depth=depth_cap,
        delay_seconds=delay,
        sleep=sleep,
        scope_prefix=scope_prefix(start_url),
        job=job,
    )


async def _sitemap(client: httpx.AsyncClient, start_url: str) -> str | None:
    """The sitemap at the conventional path, or `None` if there is not one.

    A non-success response is not an error: most documentation sites have no
    sitemap at exactly `/sitemap.xml`, and a crawl that refused to start without
    one would refuse to crawl a site that is perfectly crawlable by links.
    """
    url = str(httpx.URL(start_url).join(SITEMAP_PATH))
    try:
        response = await client.get(url)
    except Exception as exc:  # noqa: BLE001 - a missing sitemap is not a failure
        _log.info("sitemap unavailable", extra={"error": type(exc).__name__})
        return None
    if not response.is_success:
        _log.info("no sitemap at the conventional path", extra={"status": response.status_code})
        return None
    return response.text


async def _record_source_outcome(
    session: AsyncSession, source_id: uuid.UUID, result: CrawlResult
) -> None:
    """Stamp the source with what its last crawl did.

    `error` is reserved for a crawl that produced nothing usable. A crawl that
    indexed forty pages and failed two is `active` with a `completed_with_errors`
    job — the job carries the detail, and marking the whole source `error` would
    make a partial outage look like a broken registration.

    The row is re-read by id rather than carried in from the caller: the crawl
    committed a transaction per page, so the caller's instance is expired, and an
    attribute write on an expired instance is database IO in a place that cannot
    await it.
    """
    source = await session.get(Source, source_id)
    if source is None:  # pragma: no cover - the row was read at the top of the run
        return
    source.last_crawl_at = datetime.now(UTC)
    if result.status == "failed" or result.counts.indexed == 0:
        source.status = "error"
    elif source.status != "disabled":
        source.status = "active"
    await session.commit()


async def start_and_schedule(
    session: AsyncSession,
    *,
    source: Source,
    client: httpx.AsyncClient | None = None,
    store: VectorStore | None = None,
) -> CrawlJob:
    """Create the job row, commit it, and hand the crawl to a background task.

    The row is committed here rather than by the crawl, because the caller
    returns it in the response body: a job id that only exists inside an
    uncommitted transaction is an id a client can read and then never find.
    """
    job = await start_crawl_job(
        session,
        source_id=source.id,
        target_url=source.start_url,
        scope_prefix=scope_prefix(source.start_url),
    )
    await session.commit()
    schedule_crawl(source_id=source.id, job=job, client=client, store=store)
    return job


__all__ = [
    "SITEMAP_PATH",
    "cancel_all",
    "crawl_source",
    "in_flight",
    "reclaim_orphaned_jobs",
    "schedule_crawl",
    "start_and_schedule",
]
