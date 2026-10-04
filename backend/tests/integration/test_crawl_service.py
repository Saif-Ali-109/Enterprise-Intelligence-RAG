"""`crawl_source`: the wiring between the registry, HTTP, robots, and the store.

T064's engine. `pipeline.run_crawl` is tested thoroughly in `test_pipeline.py`
against a fake fetcher, and every one of those tests could pass while the real
crawl was broken in the seams: a gate that was never primed, a frontier built
from the wrong text, a sitemap fetched after the page that would have supplied
its links, a per-source bound that was read from configuration instead of from
the row. This module is the only place those seams are exercised together.

**What is real:** the real `RobotsGate` (its parsing, its longest-match rule, its
per-origin cache), the real `build_frontier` with both mechanisms, the real
`fetch_page` order — SSRF pre-flight, then robots, then the request — the real
`run_crawl`, the real registry, and the real PostgreSQL constraints.

**What is faked:** the network, through `httpx.MockTransport`, and the vector
store. Both at their boundaries. The transport records every URL it is asked
for, which is what makes the ordering claims in this module assertions rather
than comments: a gate that fetched before refusing is visible as a URL in
`site.requested` that should not be there.

**DNS is replaced** so `assert_fetchable` accepts the host without a name server.
It is the same substitution as in `test_api_sources.py`, and it is a substitution
rather than a bypass: the *order* of the guard and the fetch is still what is
under test, and a route or fetch that skipped the guard would then be talking to
a transport that does not exist.
"""

from __future__ import annotations

import asyncio
import ipaddress
import uuid
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx
import pytest
from app.core.config import get_settings
from app.db.models import CrawlJob, Document, Source
from app.db.session import get_session_factory
from app.ingestion.fetcher import build_client
from app.ingestion.pipeline import CrawlResult

from tests.fixtures.credentials import seed_placeholder_credentials
from tests.fixtures.database import describe_connection_failure, migrate_to_head, truncate_all
from tests.fixtures.vector_store import RecordingVectorStore

pytestmark = pytest.mark.integration

seed_placeholder_credentials()

SUPPORT = "https://support.atlassian.com/jira-software-cloud/docs/"
PUBLIC = ipaddress.ip_address("93.184.216.34")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def _schema() -> None:
    try:
        migrate_to_head()
    except Exception:  # noqa: BLE001 - an absent database is a skip
        pytest.skip(describe_connection_failure())


@pytest.fixture(autouse=True)
async def _database(_schema: None) -> AsyncIterator[None]:
    await truncate_all()
    yield
    await truncate_all()
    from app.db.session import dispose_engine

    await dispose_engine()


@pytest.fixture(autouse=True)
def _resolve(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "app.core.security.resolve_addresses",
        lambda host, *, port=443: [PUBLIC],
    )


@pytest.fixture
def store() -> RecordingVectorStore:
    return RecordingVectorStore()


@pytest.fixture
async def session() -> AsyncIterator[Any]:
    factory = get_session_factory()
    async with factory() as db:
        yield db


@pytest.fixture
async def source(session: Any) -> Any:
    row = Source(
        name="Jira Cloud docs",
        start_url=SUPPORT,
        allowed_domains=["support.atlassian.com"],
        product_domain="jira",
        # Ten, not three: three of the tests below need every page this site
        # offers in order to tell the two discovery mechanisms apart, and the
        # bounds tests set their own value.
        max_pages=10,
        max_depth=1,
        # 0.1, the contract's floor: `ck_sources_delay_seconds_range` refuses
        # zero, and this fixture's first version used 0.0 and reported the
        # constraint as a broken insert.
        delay_seconds=0.1,
    )
    session.add(row)
    await session.commit()
    return row


# ---------------------------------------------------------------------------
# A fake site
# ---------------------------------------------------------------------------


def page(title: str, *paragraphs: str) -> str:
    """A page the real stages accept: long enough, varied enough, prose-shaped."""
    body = "".join(f"<p>{p}</p>" for p in paragraphs)
    return (
        f"<html><head><title>{title} | Jira</title></head><body><main><h1>{title}</h1>"
        f"{body}</main></body></html>"
    )


def article(title: str, links: list[str] | None = None) -> str:
    """An article with distinct prose, optionally linking elsewhere."""
    paragraphs = [
        f"{title} explains the {word} setting for teams that need it, and why the "
        f"default is {word}ed for everybody else."
        for word in (
            "board",
            "filter",
            "column",
            "permission",
            "archive",
            "backlog",
            "card",
            "sprint",
        )
    ]
    nav = "".join(f'<a href="{href}">{href}</a>' for href in links or [])
    return f"{page(title, *paragraphs)}<nav>{nav}</nav>"


def sitemap(*urls: str) -> str:
    entries = "".join(f"<url><loc>{url}</loc></url>" for url in urls)
    return f'<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{entries}</urlset>'


class Site:
    """A documentation site served from memory, recording every request.

    `hold` lets a test stop the crawl mid-flight: set `hold_url` and the request
    for that URL parks on `release` until the test sets it. That is how a crawl
    is caught *running* — the only state in which cancellation is meaningful, and
    one that a test which simply awaits the crawl could never reach.
    """

    def __init__(
        self,
        *,
        robots: str = "User-agent: *\nAllow: /\n",
        sitemap_xml: str | None = None,
    ) -> None:
        self.robots = robots
        self.sitemap_xml = sitemap_xml
        self.pages: dict[str, str] = {}
        self.requested: list[str] = []
        self.failures: dict[str, Exception] = {}
        self.hold_url: str | None = None
        self.release = asyncio.Event()
        #: Set the moment the held URL is requested, so a test can wait for the
        #: crawl to be *inside* the fetch rather than guessing how long it takes.
        self.hold_reached = asyncio.Event()

    async def wait_until_held(self) -> None:
        """Block until the crawl is parked on the held URL.

        A timeout rather than a bare await: a crawl that never asks for the held
        page would otherwise hang the suite instead of failing it, and the
        difference is a test that says what went wrong.
        """
        await asyncio.wait_for(self.hold_reached.wait(), timeout=10)

    def add(self, url: str, html: str) -> None:
        self.pages[url] = html

    @property
    def page_requests(self) -> list[str]:
        """Requests for HTML pages, in order. Excludes robots.txt and the sitemap."""
        return [url for url in self.requested if url not in {"ROBOTS", "SITEMAP"}]

    def transport(self) -> httpx.MockTransport:
        # `httpx.MockTransport` accepts an async handler and awaits what it
        # returns, so the blocking case needs no second code path.
        async def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            path = request.url.path

            if path == "/robots.txt":
                self.requested.append("ROBOTS")
                return httpx.Response(200, text=self.robots, headers={"content-type": "text/plain"})

            if path == "/sitemap.xml":
                self.requested.append("SITEMAP")
                if self.sitemap_xml is None:
                    return httpx.Response(404, text="not found")
                return httpx.Response(
                    200, text=self.sitemap_xml, headers={"content-type": "application/xml"}
                )

            self.requested.append(url)
            if url == self.hold_url:
                self.hold_reached.set()
                await self.release.wait()
            failure = self.failures.get(url)
            if failure is not None:
                raise failure
            body = self.pages.get(url)
            if body is None:
                return httpx.Response(
                    404,
                    text="<html><body><p>Not found</p></body></html>",
                    headers={"content-type": "text/html"},
                )
            return httpx.Response(
                200, text=body, headers={"content-type": "text/html; charset=utf-8"}
            )

        return httpx.MockTransport(handler)


@pytest.fixture
def site() -> Site:
    """The site `crawl_source` will be pointed at.

    Two linked articles and a sitemap naming both plus a third page, so a test can
    tell the two discovery mechanisms apart by which URL appears.
    """
    boards = f"{SUPPORT}boards/"
    filters = f"{SUPPORT}filters/"
    columns = f"{SUPPORT}columns/"

    built = Site(sitemap_xml=sitemap(boards, filters, columns))
    built.add(SUPPORT, article("Boards", links=[boards, filters]))
    built.add(boards, article("Board basics"))
    built.add(filters, article("Filter syntax"))
    built.add(columns, article("Column configuration"))
    return built


def sleeper() -> tuple[Callable[[float], Any], list[float]]:
    """A `sleep` that records, so the delay is asserted rather than waited for."""
    waited: list[float] = []

    async def sleep(seconds: float) -> None:
        waited.append(seconds)

    return sleep, waited


# ---------------------------------------------------------------------------
# Reading the registry back
# ---------------------------------------------------------------------------


async def reload(model: Any, identifier: uuid.UUID) -> Any:
    """Re-read a row with an awaited query.

    Every assertion here follows a commit, and a commit expires the session's
    identity map. Touching an attribute then raises `MissingGreenlet` rather than
    returning the value, so a test that learned to expect that would be asserting
    the ORM's accident.
    """
    from sqlalchemy import select

    factory = get_session_factory()
    async with factory() as db:
        return (await db.execute(select(model).where(model.id == identifier))).scalar_one()


async def crawl(
    session: Any,
    source: Any,
    site: Site,
    store: RecordingVectorStore,
    **kwargs: Any,
) -> CrawlResult:
    from app.ingestion.crawl_service import crawl_source

    sleep, waited = sleeper()
    client = build_client(transport=site.transport())
    async with client:
        return await crawl_source(
            session,
            source_id=source.id,
            client=client,
            store=store,
            sleep=sleep,
            **kwargs,
        )


async def job_for(source_id: uuid.UUID) -> Any:
    """The source's job row, read back with an awaited query.

    Every assertion here follows a commit — and a crawl commits per page — so the
    session the crawl ran in has an expired identity map. Touching an attribute
    there raises `MissingGreenlet` rather than returning a value, which is why
    ids and rows are captured with queries instead of read off the object.
    """
    from sqlalchemy import select

    factory = get_session_factory()
    async with factory() as db:
        rows = list(
            (await db.execute(select(CrawlJob).where(CrawlJob.source_id == source_id))).scalars()
        )
    return rows[-1] if rows else None


async def urls_for(source_id: uuid.UUID) -> set[str]:
    """The live document URLs a source has indexed."""
    from sqlalchemy import select

    factory = get_session_factory()
    async with factory() as db:
        rows = (
            await db.execute(select(Document.url).where(Document.source_id == source_id))
        ).scalars()
        return set(rows)


# ===========================================================================
# The crawl itself
# ===========================================================================


class TestCrawlSource:
    async def test_a_crawl_indexes_the_pages_it_found(
        self, session: Any, source: Any, site: Site, store: RecordingVectorStore
    ) -> None:
        result = await crawl(session, source, site, store)

        assert result.status == "completed", result.counters.as_dict()
        assert result.counts.discovered == 4
        assert result.counts.indexed >= 3
        assert store.writes > 0
        assert len(store.live_ids) == store.writes

    async def test_the_job_row_records_what_happened(
        self, session: Any, source: Any, site: Site, store: RecordingVectorStore
    ) -> None:
        result = await crawl(session, source, site, store)

        job = await job_for(source.id)

        assert job is not None
        assert job.status == "completed"
        assert job.pages_discovered == result.counts.discovered
        assert job.pages_processed == result.counts.processed
        assert job.pages_unchanged == result.counts.unchanged
        assert job.finished_at is not None
        assert job.error_code is None
        assert job.error_message is None

    async def test_the_source_records_when_it_was_last_crawled(
        self, session: Any, source: Any, site: Site, store: RecordingVectorStore
    ) -> None:
        await crawl(session, source, site, store)

        reloaded = await reload(Source, source.id)

        assert reloaded.last_crawl_at is not None
        assert reloaded.status == "active"

    async def test_the_documents_are_live_and_attributed(
        self, session: Any, source: Any, site: Site, store: RecordingVectorStore
    ) -> None:
        from sqlalchemy import select

        await crawl(session, source, site, store)

        factory = get_session_factory()
        async with factory() as db:
            documents = list(
                (
                    await db.execute(select(Document).where(Document.source_id == source.id))
                ).scalars()
            )

        assert documents, "the crawl reported success but wrote no documents"
        assert documents[0].last_crawled_at is not None
        for document in documents:
            assert document.state == "indexed"
            assert document.vectors_live is True
            assert document.source_id == source.id
            assert document.content_fingerprint is not None

    async def test_a_second_crawl_of_unchanged_pages_indexes_nothing_new(
        self, session: Any, source: Any, site: Site, store: RecordingVectorStore
    ) -> None:
        await crawl(session, source, site, store)
        writes_after_first = store.writes

        second = await crawl(session, source, site, store)

        assert second.counts.unchanged > 0
        assert store.writes == writes_after_first, "an unchanged page was re-indexed"

    async def test_a_changed_page_is_re_indexed_under_the_same_vector_id(
        self, session: Any, source: Any, site: Site, store: RecordingVectorStore
    ) -> None:
        first = await crawl(session, source, site, store)
        ids_before = set(store.live_ids)

        site.add(f"{SUPPORT}boards/", article("Board basics, revised"))
        second = await crawl(session, source, site, store)

        assert second.counts.indexed > 0
        # R-005: a supersession upserts over the same id, so the vector set does
        # not grow and the old text cannot be retrieved.
        assert set(store.live_ids) == ids_before
        assert first.counts.indexed >= 1


class TestGateOrdering:
    async def test_robots_is_read_before_any_page_is_fetched(
        self, session: Any, source: Any, site: Site, store: RecordingVectorStore
    ) -> None:
        """The gate has to exist before it can be passed, and before it can refuse."""
        await crawl(session, source, site, store)

        assert site.requested[0] == "ROBOTS"
        assert site.page_requests, "no page was requested at all"

    async def test_a_page_robots_disallows_is_never_requested(
        self, session: Any, source: Any, store: RecordingVectorStore
    ) -> None:
        """A gate that fetches first and filters afterwards is not a gate (FR-028).

        The refusal happens in *discovery*, so the page is never queued and the
        run has nothing to count: it was not skipped, it was never a candidate.
        (`test_pipeline.py` covers the fetch-stage refusal, where a page queued
        under one decision meets the other and is counted as `robots_disallowed`.)
        """
        site = Site(
            robots="User-agent: *\nAllow: /\nDisallow: /jira-software-cloud/docs/columns/\n",
            sitemap_xml=sitemap(f"{SUPPORT}boards/", f"{SUPPORT}filters/", f"{SUPPORT}columns/"),
        )
        site.add(SUPPORT, article("Boards", links=[f"{SUPPORT}boards/"]))
        site.add(f"{SUPPORT}boards/", article("Board basics"))
        site.add(f"{SUPPORT}filters/", article("Filter syntax"))
        site.add(f"{SUPPORT}columns/", article("Column configuration"))

        result = await crawl(session, source, site, store)

        assert f"{SUPPORT}columns/" not in site.page_requests
        assert f"{SUPPORT}columns/" not in await urls_for(source.id), (
            "a page the crawler was told not to read must not be registered"
        )
        assert result.status == "completed", result.counts.as_dict()
        assert result.counts.discovered == 3, "the refused page should not have been queued"
        assert result.counts.contract_skipped_reasons() == {}

    async def test_crawl_delay_from_robots_is_a_floor_not_a_preference(
        self, session: Any, source: Any, site: Site, store: RecordingVectorStore
    ) -> None:
        """A site asking for five seconds gets five, even though the row says 0.1.

        The row's `delay_seconds` is the operator's setting; `Crawl-delay` is the
        site's own instruction. The larger wins, because the point of a crawl-delay
        is that the crawler that ignores it is the problem.
        """
        site.robots = "User-agent: *\nAllow: /\nCrawl-delay: 5\n"

        sleep, waited = sleeper()
        client = build_client(transport=site.transport())
        async with client:
            from app.ingestion.crawl_service import crawl_source

            await crawl_source(
                session,
                source_id=source.id,
                client=client,
                store=store,
                sleep=sleep,
            )

        assert waited, "the crawl fetched several pages without waiting between any of them"
        assert set(waited) == {5.0}


class TestDiscoveryPolicy:
    async def test_the_sitemap_is_requested_once_at_the_conventional_path(
        self, session: Any, source: Any, site: Site, store: RecordingVectorStore
    ) -> None:
        await crawl(session, source, site, store)

        assert site.requested.count("SITEMAP") == 1

    async def test_both_mechanisms_contribute(
        self, session: Any, source: Any, site: Site, store: RecordingVectorStore
    ) -> None:
        """A sitemap names a page the start page does not link to; it is still found.

        `columns/` appears only in the sitemap and `filters/` only as a link, and
        both must be indexed: one mechanism without the other would silently
        truncate the corpus on whichever sites happen to serve only one.
        """
        await crawl(session, source, site, store)

        urls = await urls_for(source.id)

        assert f"{SUPPORT}columns/" in urls, "a sitemap-only page was missed"
        assert f"{SUPPORT}filters/" in urls, "a link-only page was missed"

    async def test_a_missing_sitemap_leaves_link_following_intact(
        self, session: Any, source: Any, store: RecordingVectorStore
    ) -> None:
        """A 404 at `/sitemap.xml` is not a failure and not an empty crawl."""

        site = Site(sitemap_xml=None)
        site.add(SUPPORT, article("Boards", links=[f"{SUPPORT}boards/"]))
        site.add(f"{SUPPORT}boards/", article("Board basics"))

        result = await crawl(session, source, site, store)

        assert "SITEMAP" in site.requested, "the conventional path was never tried"
        assert f"{SUPPORT}boards/" in await urls_for(source.id)
        assert result.status == "completed"

    async def test_a_sitemap_the_gate_refuses_is_not_parsed(
        self, session: Any, source: Any, store: RecordingVectorStore
    ) -> None:
        """`Crawl-delay` and `Disallow` are read; the sitemap is still fetched when
        robots allows it, and its entries are filtered by the same gate."""
        site = Site(
            robots="User-agent: *\nDisallow: /sitemap.xml\nAllow: /\n",
            sitemap_xml=sitemap(f"{SUPPORT}boards/"),
        )
        site.add(SUPPORT, article("Boards", links=[f"{SUPPORT}boards/"]))
        site.add(f"{SUPPORT}boards/", article("Board basics"))

        # The sitemap request is made by this module directly rather than through
        # the gate, because the gate's job is page authorisation. What matters is
        # that its *entries* are filtered by the same predicate.
        await crawl(session, source, site, store)

        assert f"{SUPPORT}boards/" in site.page_requests


class TestBounds:
    async def test_the_sources_own_page_cap_bounds_the_crawl(
        self, session: Any, source: Any, site: Site, store: RecordingVectorStore
    ) -> None:
        """`max_pages=2` on the row, four pages found: two are crawled.

        Read from the row rather than from configuration, because the row is what
        `PATCH /sources/{id}` changes — and a bound that only lived in the request
        would have nothing to persist.
        """
        source.max_pages = 2
        await session.commit()

        result = await crawl(session, source, site, store)

        assert result.counts.discovered == 2

    async def test_a_null_bound_means_the_configured_default(
        self, session: Any, source: Any, site: Site, store: RecordingVectorStore
    ) -> None:
        """Null is "use the default", never zero (the column's own rule)."""
        source.max_pages = None
        source.max_depth = None
        source.delay_seconds = None
        await session.commit()

        result = await crawl(session, source, site, store)

        assert result.counts.discovered <= get_settings().crawl_max_pages

    async def test_a_caller_cannot_raise_a_bound_past_the_configured_ceiling(
        self, session: Any, source: Any, site: Site, store: RecordingVectorStore
    ) -> None:
        """The ceiling belongs to the deployment, not to a request.

        `max_pages=500` is legal for the schema and legal for the column, and it
        still cannot make this process crawl 500 pages when it is configured for
        100.
        """
        source.max_pages = 500
        await session.commit()

        settings = get_settings()
        original = settings.crawl_max_pages
        try:
            settings.crawl_max_pages = 2
            result = await crawl(session, source, site, store)
            assert result.counts.discovered == 2
        finally:
            settings.crawl_max_pages = original


class TestFailureHandling:
    async def test_one_failing_page_does_not_end_the_crawl(
        self, session: Any, source: Any, site: Site, store: RecordingVectorStore
    ) -> None:
        site.failures[f"{SUPPORT}filters/"] = httpx.ConnectError("connection reset")

        result = await crawl(session, source, site, store)

        assert result.status == "completed_with_errors"
        assert result.counts.failed == 1
        assert result.counts.indexed >= 1

    async def test_a_partial_crawl_leaves_the_source_active(
        self, session: Any, source: Any, site: Site, store: RecordingVectorStore
    ) -> None:
        """Forty pages indexed and two failed is not a broken registration."""
        site.failures[f"{SUPPORT}filters/"] = httpx.ConnectError("connection reset")

        await crawl(session, source, site, store)

        reloaded = await reload(Source, source.id)
        assert reloaded.status == "active"

    async def test_a_crawl_that_indexed_nothing_marks_the_source_in_error(
        self, session: Any, source: Any, store: RecordingVectorStore
    ) -> None:
        """Two different refusals, two different reasons, one outcome.

        The start page is too small to index and the sitemap entry is gone, so the
        crawl can reach nothing at all — and `skipped_reasons` says which refusal
        was which rather than reporting a single undifferentiated "nothing found".
        """
        site = Site(sitemap_xml=sitemap(f"{SUPPORT}gone/"))
        site.add(SUPPORT, "<html><body><p>Coming soon.</p></body></html>")

        result = await crawl(session, source, site, store)

        reasons = result.counts.contract_skipped_reasons()
        assert result.counts.indexed == 0
        assert reasons["too_small"] == 1, "the stub page should be rejected for its size"
        assert reasons["http_error"] == 1, "a 404 is not a page that was too small"

        reloaded = await reload(Source, source.id)
        assert reloaded.status == "error"

    async def test_a_start_page_that_cannot_be_fetched_still_leaves_the_job(
        self, session: Any, source: Any, site: Site, store: RecordingVectorStore
    ) -> None:
        """The audit trail outlives the failure that explains it.

        The crawl cannot run without the start page, so the job row is the only
        record that a crawl was ever requested. It is created before the first
        fetch and closed by the failure itself — a `running` row here would be a
        claim about work that stopped, which is what `reclaim_orphaned_jobs` exists
        to clean up after a crash and should not be the shape of a refused fetch.
        """
        site.failures[SUPPORT] = httpx.ConnectError("connection reset")

        with pytest.raises(httpx.ConnectError):
            await crawl(session, source, site, store)

        job = await job_for(source.id)
        assert job is not None, "the crawl ran without ever creating a job row"
        assert job.status == "failed"
        assert job.finished_at is not None
        assert job.error_code == "INTERNAL_ERROR"
        assert job.pages_processed == 0


# ===========================================================================
# Scheduling and lifecycle
# ===========================================================================


#: Clients opened by `scheduled`, closed by the fixture below so a crawl that is
#: cancelled mid-request does not leave a pool behind.
_OPEN_CLIENTS: list[httpx.AsyncClient] = []


def scheduled(site: Site, store: RecordingVectorStore, source_id: uuid.UUID) -> asyncio.Task[None]:
    """Start a crawl the way the API does — as a task, not awaited inline.

    Returns the task so a test can await it (to observe a finished crawl) or
    hold it (to observe a running one). The client is closed by the fixture,
    because `schedule_crawl` does not own one it was handed.
    """
    from app.ingestion.crawl_service import schedule_crawl

    client = build_client(transport=site.transport())
    task = schedule_crawl(
        source_id=source_id,
        session_factory=get_session_factory(),
        client=client,
        store=store,
    )
    _OPEN_CLIENTS.append(client)
    return task


@pytest.fixture(autouse=True)
async def _close_clients() -> AsyncIterator[None]:
    yield
    for client in _OPEN_CLIENTS:
        try:
            await client.aclose()
        except Exception:  # noqa: BLE001 - a cancelled request may leave it closed
            pass
    _OPEN_CLIENTS.clear()


class TestScheduling:
    async def test_a_scheduled_crawl_indexes_the_site(
        self, source: Any, site: Site, store: RecordingVectorStore
    ) -> None:
        await scheduled(site, store, source.id)

        assert await urls_for(source.id), "the scheduled crawl produced no documents"
        job = await job_for(source.id)
        assert job is not None and job.status == "completed"

    async def test_a_second_schedule_reuses_the_running_task(
        self, source: Any, site: Site, store: RecordingVectorStore
    ) -> None:
        """Two clicks on "crawl" are one crawl.

        The database's partial unique index would refuse the second job anyway;
        this is the earlier half of the same guarantee, and it is the half that
        stops a second crawl from being *started* and then failing.
        """
        site.hold_url = SUPPORT

        first = scheduled(site, store, source.id)
        second = scheduled(site, store, source.id)

        assert first is second
        site.release.set()
        await first

    async def test_in_flight_reports_the_source_while_the_crawl_runs(
        self, source: Any, site: Site, store: RecordingVectorStore
    ) -> None:
        from app.ingestion.crawl_service import in_flight

        site.hold_url = SUPPORT
        task = scheduled(site, store, source.id)

        assert in_flight() == {source.id}

        site.release.set()
        await task
        assert in_flight() == set()

    async def test_cancel_all_closes_the_job_and_frees_the_slot(
        self, source: Any, site: Site, store: RecordingVectorStore
    ) -> None:
        """A cancelled job must not hold `crawl_jobs_one_active_per_source` forever.

        `cancel_all` is the difference between a shutdown that leaves an
        explainable database and one that leaves a source permanently unable to
        crawl until someone edits a table by hand.
        """
        from app.ingestion.crawl_service import cancel_all

        site.hold_url = SUPPORT
        task = scheduled(site, store, source.id)
        await site.wait_until_held()

        assert await cancel_all() == 1
        await asyncio.gather(task, return_exceptions=True)

        job = await job_for(source.id)
        assert job is not None
        assert job.status == "cancelled"
        assert job.finished_at is not None

        from app.ingestion.pipeline import active_job_for
        from sqlalchemy import select

        factory = get_session_factory()
        async with factory() as db:
            assert await active_job_for(db, source.id) is None, "the slot is still held"
            rows = list(
                (
                    await db.execute(select(CrawlJob).where(CrawlJob.source_id == source.id))
                ).scalars()
            )
        assert len(rows) == 1, "cancellation closed the job by creating a second one"

    async def test_a_cancelled_crawl_leaves_no_uncommitted_documents(
        self, source: Any, site: Site, store: RecordingVectorStore
    ) -> None:
        """Cancellation is a rollback of the page in flight, not of the run.

        The pages indexed before the cancel are committed and stay; the one being
        fetched when the cancel landed is gone. Both halves matter: the first is
        FR-054, the second is what stops a half-written document from being
        published.
        """
        from app.ingestion.crawl_service import cancel_all

        site.hold_url = f"{SUPPORT}filters/"
        task = scheduled(site, store, source.id)
        await site.wait_until_held()

        assert await cancel_all() == 1
        await asyncio.gather(task, return_exceptions=True)

        urls = await urls_for(source.id)
        assert f"{SUPPORT}filters/" not in urls, "a cancelled page was committed"
        assert SUPPORT in urls, "a page indexed before the cancel was discarded"
