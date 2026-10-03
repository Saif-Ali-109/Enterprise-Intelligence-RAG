"""The ingestion pipeline against a real database and a recording store.

T058, T059, T060, T061, T062 and the behaviour tasks T046 (lifecycle),
T047 (concurrency), T048 (deletion precedence).

**What is real here:** PostgreSQL, through the real ORM models, the real
constraints — including the partial unique index that makes FR-055 a database
guarantee — and the real session discipline the orchestrator uses.

**What is a fake:** the vector store and the network. The store is
`RecordingVectorStore`, which keeps records and counts calls; the fetcher is a
mapping from URL to HTML. Both are fakes because the properties under test are
about *decisions* — what gets written, what gets skipped, what survives a
deletion — and those decisions are observable only at the store's boundary.
A live index would make these assertions cost money and still not prove
anything the fake does not.

**A note on re-reading rows.** Several tests assert on state *after* a
rollback, which is where the interesting states are produced. A rollback
expires the session's identity map, so every assertion re-reads its row with an
awaited query rather than touching a stale attribute: touching one raises
`MissingGreenlet` instead of returning the value, and a test that learned to
expect that would be asserting the ORM's accident rather than the pipeline's
behaviour.

No vendor is contacted, so this module seeds placeholder credentials if the
environment has none. Unlike `tests/unit/conftest.py` this cannot make a
credential *presence* check wrong, because nothing here asks whether a real
credential exists.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import pytest
from app.core.errors import CrawlAlreadyRunning, DocumentDeleted, RobotsDisallowed
from app.core.logging import get_logger
from app.db.models import CrawlJob, Document, DocumentUnit, Source
from app.db.session import create_all, dispose_engine, get_engine, get_session_factory
from app.ingestion.page_pipeline import ingest_page
from app.ingestion.pipeline import (
    active_job_for,
    delete_document,
    get_document,
    index_page,
    run_crawl,
    start_crawl_job,
)
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.fixtures.credentials import seed_placeholder_credentials
from tests.fixtures.vector_store import RecordingVectorStore

pytestmark = pytest.mark.integration

_log = get_logger("tests.pipeline")

seed_placeholder_credentials()

SUPPORT = "https://support.atlassian.com/jira-software-cloud/docs/"
ALLOWED = ["support.atlassian.com"]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


async def _postgres_reachable() -> bool:
    try:
        async with get_engine().connect() as connection:
            await connection.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 - the absence of a database is a skip
        _log.warning("postgres unavailable", extra={"error": type(exc).__name__})
        return False
    return True


@pytest.fixture(autouse=True)
async def _database() -> AsyncIterator[None]:
    """A schema that exists, and no rows that do not.

    Function-scoped rather than module-scoped because
    `asyncio_default_fixture_loop_scope` is `function`: an asyncpg pool belongs
    to the loop that opened it, so a module-scoped fixture would hand a later
    test's loop a pool bound to an earlier loop's connections. `create_all` is
    idempotent and cheap on an empty schema, and `drop_all` in teardown leaves
    the developer's database as this module found it.
    """
    if not await _postgres_reachable():
        pytest.skip(
            "PostgreSQL is not reachable; the pipeline's guarantees live in its constraints"
        )
    await create_all()
    # Truncate before each test, so one test's crawl job cannot block the next.
    # The partial unique index on active crawl jobs makes this necessary rather
    # than tidy: a leftover `running` job is a legitimate database state that
    # would make the *next* test's crawl fail for the wrong reason.
    async with get_engine().begin() as connection:
        await connection.execute(text("TRUNCATE sources CASCADE"))
    yield
    # Truncate, do not drop. Another suite may run after this one and expects
    # the tables to exist — `routes_config` counts documents and units to
    # report the corpus size, and a suite that leaves the schema behind fails
    # that test for a reason that has nothing to do with either of them.
    async with get_engine().begin() as connection:
        await connection.execute(text("TRUNCATE sources CASCADE"))
    # The engine is a process-wide singleton whose asyncpg pool belongs to the
    # event loop that opened it. This suite's loop is per-function, so without
    # this the next test inherits connections bound to a closed loop and every
    # query fails in a way that looks like "PostgreSQL is unreachable".
    await dispose_engine()


@pytest.fixture
def store() -> RecordingVectorStore:
    return RecordingVectorStore()


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    factory: async_sessionmaker[AsyncSession] = get_session_factory()
    async with factory() as db:
        yield db
        await db.rollback()


@dataclass(frozen=True, slots=True)
class RegisteredSource:
    """A registered source with its identity captured before any rollback.

    Rollback expires every attribute of every object in the session, and a
    sync attribute read afterwards raises `MissingGreenlet` instead of
    returning the id. Every test here that exercises a failure path — mine or
    the orchestrator's — hits exactly that. The row is kept so a test that
    needs a live attribute can ask the ORM for it; the id and the start URL are
    kept as plain strings because the pipeline needs them *after* a rollback,
    where the ORM cannot serve them.
    """

    row: Source
    id: uuid.UUID
    start_url: str

    @property
    def allowed_domains(self) -> list[str]:
        return list(self.row.allowed_domains)


@pytest.fixture
async def source(session: AsyncSession) -> RegisteredSource:
    row = Source(
        name="Jira Cloud docs",
        start_url=SUPPORT,
        allowed_domains=ALLOWED,
        product_domain="jira",
    )
    session.add(row)
    await session.commit()
    return RegisteredSource(row=row, id=row.id, start_url=row.start_url)


# ---------------------------------------------------------------------------
# Doubles for the network
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FakeResponse:
    text: str
    headers: dict[str, str] = field(
        default_factory=lambda: {"content-type": "text/html; charset=utf-8"}
    )


def page(title: str, *paragraphs: str) -> str:
    """A realistic enough documentation page for the real stages to accept.

    Long enough to clear the boilerplate floor and varied enough to pass the
    repetition check, because a fixture that only satisfies `lxml` proves nothing
    about the floor T051 exists to enforce.
    """
    body = "".join(f"<p>{p}</p>" for p in paragraphs)
    return f"<html><head><title>{title} | Jira</title></head><body><main><h1>{title}</h1>{body}</main></body></html>"


def alpha_page(title: str) -> str:
    return page(
        title,
        "Boards let your team see the work that matters and the work that is blocked.",
        "Each board collects issues from one or more filters, and you can move a card between columns as it progresses.",
        "Board configuration lives with the board itself, so a change to a filter does not silently rewrite history.",
        "Permissions are inherited from the project the board draws its issues from.",
        "Archived boards stop appearing in search but their issues remain in the backlog.",
    )


def alpha_page_v2(title: str) -> str:
    return page(
        title,
        "Boards let your team see the work that matters, the work that is blocked, and who owns each piece.",
        "Each board collects issues from one or more filters, and you can move a card between columns as it progresses.",
        "Board configuration lives with the board itself, so a change to a filter does not silently rewrite history.",
        "Permissions are inherited from the project the board draws its issues from, and can be narrowed per board.",
        "Archived boards stop appearing in search but their issues remain in the backlog.",
    )


def long_page(title: str, paragraphs: int) -> str:
    """A page long enough to be split into several units.

    `alphas` differ per paragraph on purpose: a repetition check that a real
    page would fail is a check that makes this test lie about supersession.
    """
    alphabet = "abcdefghijklmnopqrstuvwxyz"
    body = [
        f"Section point {i} of the {title} documentation explains "
        f"{alphabet[i % 26]} specific setting in enough words to be worth indexing "
        f"({alphabet[(i * 7) % 26]}{alphabet[(i * 13) % 26]})."
        for i in range(paragraphs)
    ]
    return page(title, *body)


def short_page(title: str) -> str:
    return page(title, "Just one short paragraph of prose that will never be indexed on its own.")


class Fetcher:
    """A fetcher that serves HTML and can be told to fail.

    `failures` are raised, not returned as a 500: a transport error and an HTTP
    error both have to leave the run alive, and raising covers the harsher of
    the two paths.
    """

    def __init__(
        self,
        pages: dict[str, str],
        *,
        failures: dict[str, Exception] | None = None,
        content_types: dict[str, str] | None = None,
    ) -> None:
        self.pages = pages
        self.failures = failures or {}
        self.content_types = content_types or {}
        self.requested: list[str] = []

    async def __call__(self, url: str) -> FakeResponse:
        self.requested.append(url)
        if url in self.failures:
            raise self.failures[url]
        body = self.pages.get(url)
        if body is None:
            return FakeResponse(
                text="<html><body><p>Not found</p></body></html>",
                headers={"content-type": "text/html"},
            )
        return FakeResponse(
            text=body, headers={"content-type": self.content_types.get(url, "text/html")}
        )


def candidates(*urls: str, depth: int = 1) -> list[tuple[str, int, str]]:
    return [(url, depth, "test") for url in urls]


async def run(
    session: AsyncSession,
    store: RecordingVectorStore,
    source: RegisteredSource,
    fetch: Any,
    **kwargs: Any,
) -> Any:
    kwargs.setdefault("candidates", [])
    kwargs.setdefault("delay_seconds", 0)
    return await run_crawl(session, store, source_id=source.id, fetch=fetch, **kwargs)


async def units_of(
    session: AsyncSession,
    document_id: Any,
    *,
    expect_at_least: int = 1,
) -> list[DocumentUnit]:
    """The document's units in order, asserting the page was actually split.

    `expect_at_least` exists because a fixture that stops producing several
    units fails as an assertion about supersession that is really an assertion
    about the fixture. Saying so here keeps that failure legible.
    """
    result = await session.execute(
        select(DocumentUnit)
        .where(DocumentUnit.document_id == document_id)
        .order_by(DocumentUnit.ordinal)
    )
    units = list(result.scalars())
    assert len(units) >= expect_at_least, f"fixture produced {len(units)} units"
    return units


# ---------------------------------------------------------------------------
# T058/T059: indexing and registry persistence
# ---------------------------------------------------------------------------


class TestIndexing:
    async def test_first_crawl_indexes_and_marks_live(self, session, store, source) -> None:
        url = f"{SUPPORT}boards/"
        result = await run(
            session, store, source, Fetcher({url: alpha_page("Boards")}), candidates=candidates(url)
        )

        assert result.status == "completed"
        assert result.counts.indexed == 1
        assert store.writes > 0

        document = await get_document(session, source.id, url)
        assert document is not None
        assert document.state == "indexed"
        assert document.vectors_live is True
        assert document.title == "Boards"
        assert document.product == "jira"
        assert document.language == "en"
        assert document.indexed_at is not None
        assert document.content_fingerprint

        units = await units_of(session, document.id)
        assert units, "an indexed document must have unit rows to join citations against"
        assert [u.ordinal for u in units] == list(range(len(units)))
        assert all(u.token_count > 0 for u in units)
        assert all(u.vector_id == f"{document.id}#{u.ordinal:04d}" for u in units)

    async def test_unit_rows_carry_provenance_and_measured_overlap(
        self, session, store, source
    ) -> None:
        url = f"{SUPPORT}boards/"
        await run(
            session,
            store,
            source,
            Fetcher({url: long_page("Boards", 40)}),
            candidates=candidates(url),
        )
        document = await get_document(session, source.id, url)
        assert document is not None

        units = await units_of(session, document.id)
        assert len(units) > 1, "a long page must be split into several units"
        assert all(u.heading_path is not None for u in units)
        assert all(u.block_types for u in units)
        # Overlap is measured from provenance (T060), and at least one unit of a
        # multi-unit page must repeat its predecessor's tail.
        assert any(u.overlap_tokens > 0 for u in units[1:])

    async def test_vectors_carry_the_flat_filter_metadata(self, session, store, source) -> None:
        url = f"{SUPPORT}boards/"
        await run(
            session, store, source, Fetcher({url: alpha_page("Boards")}), candidates=candidates(url)
        )
        for record in store.records.values():
            assert record.metadata["product"] == "jira"
            assert record.metadata["source_url"] == url
            assert "namespace" not in record.metadata
            assert all(not isinstance(value, dict) for value in record.metadata.values())


# ---------------------------------------------------------------------------
# T060: supersession (FR-026, SC-012)
# ---------------------------------------------------------------------------


class TestSupersession:
    async def test_identical_recrawl_writes_no_vectors(self, session, store, source) -> None:
        url = f"{SUPPORT}boards/"
        fetch = Fetcher({url: alpha_page("Boards")})
        await run(session, store, source, fetch, candidates=candidates(url))
        writes_after_first = store.writes

        second = await run(session, store, source, fetch, candidates=candidates(url))
        assert second.counts.unchanged == 1
        assert second.counts.indexed == 0
        assert store.writes == writes_after_first, "an unchanged page must not be re-embedded"

    async def test_repeated_crawls_never_accumulate_duplicates(
        self, session, store, source
    ) -> None:
        url = f"{SUPPORT}boards/"
        fetch = Fetcher({url: alpha_page("Boards")})
        for _ in range(3):
            await run(session, store, source, fetch, candidates=candidates(url))

        document = await get_document(session, source.id, url)
        assert document is not None
        units = await units_of(session, document.id)
        assert len(units) == document.unit_count == len(store.records)

    async def test_changed_content_keeps_the_same_vector_ids(self, session, store, source) -> None:
        url = f"{SUPPORT}boards/"
        await run(
            session, store, source, Fetcher({url: alpha_page("Boards")}), candidates=candidates(url)
        )
        document = await get_document(session, source.id, url)
        assert document is not None
        first_fingerprint = document.content_fingerprint
        ids_before = set(store.records)

        await run(
            session,
            store,
            source,
            Fetcher({url: alpha_page_v2("Boards")}),
            candidates=candidates(url),
        )

        document = await get_document(session, source.id, url)
        assert document is not None
        assert document.content_fingerprint != first_fingerprint
        assert set(store.records) == ids_before, "a changed page is superseded in place"
        assert store.deleted == [], "supersession overwrites; it does not delete the predecessor"

    async def test_a_shrinking_page_orphans_exactly_its_tail(self, session, store, source) -> None:
        url = f"{SUPPORT}boards/"
        await run(
            session,
            store,
            source,
            Fetcher({url: long_page("Boards", 90)}),
            candidates=candidates(url),
        )
        document = await get_document(session, source.id, url)
        assert document is not None
        units_before = await units_of(session, document.id, expect_at_least=3)
        # `alpha_page` produces a single unit, so every unit past ordinal 0 is an
        # orphan once the page shrinks.
        orphans_expected = {u.vector_id for u in units_before[1:]}

        await run(
            session,
            store,
            source,
            Fetcher({url: alpha_page("Boards")}),
            candidates=candidates(url),
        )

        document = await get_document(session, source.id, url)
        assert document is not None
        units_after = await units_of(session, document.id)
        live = set(store.records)
        assert {u.vector_id for u in units_after} == live
        assert set(store.deleted) == orphans_expected
        assert len(units_after) == document.unit_count < len(units_before)


# ---------------------------------------------------------------------------
# T061: the orchestrator (FR-025, FR-027, FR-054, FR-055)
# ---------------------------------------------------------------------------


class TestOrchestrator:
    async def test_a_failed_page_does_not_end_the_crawl(self, session, store, source) -> None:
        good = f"{SUPPORT}boards/"
        bad = f"{SUPPORT}boom/"
        fetch = Fetcher(
            {good: alpha_page("Boards")}, failures={bad: TimeoutError("connection reset")}
        )
        result = await run(session, store, source, fetch, candidates=candidates(bad, good))

        assert result.status == "completed_with_errors"
        assert result.counts.failed == 1
        assert result.counts.indexed == 1

        job = await session.get(CrawlJob, result.job_id)
        assert job is not None
        assert job.error_code is None, "the job itself succeeded; one page did not"
        assert job.pages_failed == 1

        good_doc = await get_document(session, source.id, good)
        bad_doc = await get_document(session, source.id, bad)
        assert good_doc is not None and good_doc.state == "indexed"
        assert bad_doc is not None and bad_doc.state == "failed"
        assert bad_doc.vectors_live is False, "a first crawl that failed never had vectors"

    async def test_failed_recrawl_keeps_the_previous_version_live(
        self, session, store, source
    ) -> None:
        # FR-054: the whole reason `vectors_live` is a column.
        url = f"{SUPPORT}boards/"
        await run(
            session, store, source, Fetcher({url: alpha_page("Boards")}), candidates=candidates(url)
        )
        live_before = dict(store.records)

        await run(
            session,
            store,
            source,
            Fetcher({}, failures={url: TimeoutError("reset")}),
            candidates=candidates(url),
        )

        document = await get_document(session, source.id, url)
        assert document is not None
        assert document.state == "failed"
        assert document.vectors_live is True
        assert store.records == live_before

    async def test_a_mid_run_crash_keeps_the_pages_already_committed(
        self, session, store, source
    ) -> None:
        """The transaction boundary, stated as the property it exists for.

        A crawl whose second page dies in the vector store must leave the first
        page indexed and its job row readable. This is the regression test for
        running the whole crawl in one transaction, where the first failure
        erased the run's own audit trail along with its work.
        """
        first = f"{SUPPORT}boards/"
        second = f"{SUPPORT}filters/"
        fetch = Fetcher({first: alpha_page("Boards"), second: alpha_page("Filters")})

        original_upsert = store.upsert

        async def failing_upsert(records: list[Any], *, namespace: str | None = None) -> Any:
            # Keyed on the metadata's source_url rather than the chunk text: the
            # failure is meant to be about *which document* is being written,
            # not about what its prose happens to say.
            if any(record.metadata.get("source_url") == second for record in records):
                raise RuntimeError("vector service exploded")
            return await original_upsert(records, namespace=namespace)

        store.upsert = failing_upsert  # type: ignore[method-assign]

        result = await run(session, store, source, fetch, candidates=candidates(first, second))

        assert result.status == "completed_with_errors"
        assert result.counts.indexed == 1
        assert result.counts.failed == 1

        first_doc = await get_document(session, source.id, first)
        second_doc = await get_document(session, source.id, second)
        assert first_doc is not None and first_doc.state == "indexed"
        assert first_doc.vectors_live is True
        assert second_doc is not None and second_doc.state == "failed"
        assert second_doc.vectors_live is False

        job = await session.get(CrawlJob, result.job_id)
        assert job is not None, "the job row survived the failure that ended one page"

    async def test_rejected_pages_are_counted_with_their_reason(
        self, session, store, source
    ) -> None:
        good = f"{SUPPORT}boards/"
        stub = f"{SUPPORT}stub/"
        api = f"{SUPPORT}api/"
        fetch = Fetcher(
            {good: alpha_page("Boards"), stub: short_page("Stub"), api: '{"units": []}'},
            content_types={api: "application/json"},
        )
        result = await run(session, store, source, fetch, candidates=candidates(good, stub, api))

        reasons = result.counts.skip_reasons
        assert reasons.get("non_html_content_type") == 1
        assert any(reason in reasons for reason in ("too_small", "extraction_produced_no_blocks"))
        assert result.counts.rejected == 2
        assert result.counts.indexed == 1
        assert result.status == "completed", "a rejected page is not a failed page"

    async def test_a_robots_refusal_is_recorded_as_a_skip_not_a_failure(
        self, session, store, source
    ) -> None:
        """FR-028, through the orchestrator's own accounting.

        A refusal by the site's directives is not an error: nothing was
        attempted, and the run is correct. Recording it as a failure would put
        a `ROBOTS_DISALLOWED` error code on a healthy job and make
        `completed_with_errors` the outcome of a crawl that did what it was
        told.
        """
        allowed = f"{SUPPORT}boards/"
        refused = f"{SUPPORT}/contact/sales/"
        fetch = Fetcher({allowed: alpha_page("Boards")}, failures={refused: RobotsDisallowed()})

        result = await run(
            session, store, source, fetch, candidates=candidates(allowed, refused)
        )

        assert result.status == "completed"
        assert result.counts.failed == 0
        assert result.counts.skip_reasons.get("robots_disallowed") == 1
        assert result.counts.indexed == 1

        job = await session.get(CrawlJob, result.job_id)
        assert job is not None
        assert job.error_code is None
        assert job.pages_skipped == 1
        assert (job.skipped_reasons or {}).get("skip_reasons", {}).get("robots_disallowed") == 1

        document = await get_document(session, source.id, refused)
        assert document is None, "a page the crawler was told not to read is not registered"

    async def test_page_cap_is_enforced_by_the_orchestrator(self, session, store, source) -> None:
        urls = [f"{SUPPORT}page-{i}/" for i in range(4)]
        fetch = Fetcher({u: alpha_page(f"Page {i}") for i, u in enumerate(urls)})
        result = await run(session, store, source, fetch, candidates=candidates(*urls), max_pages=2)

        assert result.counts.discovered == 2
        assert len(fetch.requested) == 2

    async def test_depth_cap_is_enforced_by_the_orchestrator(self, session, store, source) -> None:
        shallow = f"{SUPPORT}shallow/"
        deep = f"{SUPPORT}deep/nested/page/"
        fetch = Fetcher({shallow: alpha_page("Shallow"), deep: alpha_page("Deep")})
        result = await run(
            session,
            store,
            source,
            fetch,
            candidates=[(shallow, 0, "root"), (deep, 4, "link")],
            max_depth=2,
        )

        assert result.counts.discovered == 1
        assert fetch.requested == [shallow]

    async def test_delay_is_honoured_between_pages(self, session, store, source) -> None:
        urls = [f"{SUPPORT}p{i}/" for i in range(3)]
        fetch = Fetcher({u: alpha_page(f"P{i}") for i, u in enumerate(urls)})
        slept: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            slept.append(seconds)

        await run(
            session,
            store,
            source,
            fetch,
            candidates=candidates(*urls),
            delay_seconds=0.25,
            sleep=fake_sleep,
        )
        # Three pages, two gaps: the delay is between requests, never before the
        # first and never after the last.
        assert slept == [0.25, 0.25]

    async def test_second_crawl_for_one_source_is_refused_by_name(
        self, session, store, source
    ) -> None:
        first = await start_crawl_job(session, source_id=source.id, target_url=source.start_url)
        with pytest.raises(CrawlAlreadyRunning) as exc:
            await start_crawl_job(session, source_id=source.id, target_url=source.start_url)
        assert exc.value.details == {"running_job_id": str(first.id)}

    async def test_concurrent_crawls_are_impossible_at_the_database_level(
        self, session, source
    ) -> None:
        # FR-055's real guarantee. The application check above is for a good
        # error message; this is the one that holds under two processes.
        session.add(CrawlJob(source_id=source.id, target_url=source.start_url, status="running"))
        await session.commit()

        session.add(CrawlJob(source_id=source.id, target_url=source.start_url, status="running"))
        with pytest.raises(IntegrityError):
            await session.commit()
        await session.rollback()

        # And a finished job does not block the next crawl.
        result = await session.execute(select(CrawlJob).where(CrawlJob.source_id == source.id))
        rows = list(result.scalars())
        assert len(rows) == 1
        rows[0].status = "completed"
        await session.commit()
        assert await active_job_for(session, source.id) is None
        await start_crawl_job(session, source_id=source.id, target_url=source.start_url)

    async def test_job_records_what_it_was_not_able_to_do(self, session, store, source) -> None:
        url = f"{SUPPORT}boards/"
        result = await run(
            session, store, source, Fetcher({url: alpha_page("Boards")}), candidates=candidates(url)
        )
        job = await session.get(CrawlJob, result.job_id)
        assert job is not None
        assert job.pages_discovered == 1
        assert job.pages_processed == 1
        assert job.started_at is not None and job.finished_at is not None
        assert job.skipped_reasons is not None


# ---------------------------------------------------------------------------
# T062 / T048: deletion (FR-031, FR-056, SC-013)
# ---------------------------------------------------------------------------


class TestDeletion:
    async def test_deletion_removes_registry_rows_and_vectors(self, session, store, source) -> None:
        url = f"{SUPPORT}boards/"
        await run(
            session, store, source, Fetcher({url: alpha_page("Boards")}), candidates=candidates(url)
        )
        document = await get_document(session, source.id, url)
        assert document is not None
        assert store.records
        vectors_before = len(store.records)

        outcome = await delete_document(session, store, document_id=document.id)
        await session.commit()

        assert outcome.units_removed > 0
        assert outcome.vectors_removed == vectors_before
        assert store.records == {}
        assert await units_of(session, document.id, expect_at_least=0) == []

        document = await get_document(session, source.id, url)
        assert document is not None
        assert document.state == "deleted"
        assert document.tombstoned_at is not None
        assert document.vectors_live is False
        assert document.unit_count == 0

    async def test_deletion_is_idempotent(self, session, store, source) -> None:
        url = f"{SUPPORT}boards/"
        await run(
            session, store, source, Fetcher({url: alpha_page("Boards")}), candidates=candidates(url)
        )
        document = await get_document(session, source.id, url)
        assert document is not None

        first = await delete_document(session, store, document_id=document.id)
        deleted_once = list(store.deleted)
        second = await delete_document(session, store, document_id=document.id)
        await session.commit()

        assert first.already_deleted is False
        assert second.already_deleted is True
        assert second.vectors_removed == 0
        assert store.deleted == deleted_once, "delete twice must not delete twice"
        assert store.records == {}

    async def test_a_deleted_document_is_never_refetched_by_a_later_crawl(
        self, session, store, source
    ) -> None:
        url = f"{SUPPORT}boards/"
        fetch = Fetcher({url: alpha_page("Boards")})
        await run(session, store, source, fetch, candidates=candidates(url))
        document = await get_document(session, source.id, url)
        assert document is not None
        await delete_document(session, store, document_id=document.id)
        await session.commit()

        requested_before = list(fetch.requested)
        result = await run(session, store, source, fetch, candidates=candidates(url))

        assert fetch.requested == requested_before, "a tombstoned document is not fetched at all"
        assert result.counts.skip_reasons.get("tombstoned") == 1
        assert store.records == {}

    async def test_deletion_wins_over_an_in_flight_write(self, session, store, source) -> None:
        url = f"{SUPPORT}boards/"
        await run(
            session, store, source, Fetcher({url: alpha_page("Boards")}), candidates=candidates(url)
        )
        document = await get_document(session, source.id, url)
        assert document is not None
        await delete_document(session, store, document_id=document.id)
        await session.commit()

        # The write path itself refuses, which is the guard that holds for a
        # caller that never went through the orchestrator's tombstone check.
        ingestion = ingest_page(alpha_page_v2("Boards"), url=url, content_type="text/html")
        with pytest.raises(DocumentDeleted):
            await index_page(session, store, source_id=source.id, ingestion=ingestion)
        await session.rollback()

        document = await get_document(session, source.id, url)
        assert document is not None
        assert document.state == "deleted"
        assert store.records == {}

    async def test_a_deleted_document_stays_deleted_after_every_crawl(
        self, session, store, source
    ) -> None:
        url = f"{SUPPORT}boards/"
        fetch = Fetcher({url: alpha_page_v2("Boards")})
        await run(session, store, source, fetch, candidates=candidates(url))
        document = await get_document(session, source.id, url)
        assert document is not None
        await delete_document(session, store, document_id=document.id)
        await session.commit()

        for _ in range(3):
            await run(session, store, source, fetch, candidates=candidates(url))

        document = await session.get(Document, document.id)
        assert document is not None
        assert document.state == "deleted"
        assert store.records == {}

    async def test_vectors_are_not_removed_on_any_other_transition(
        self, session, store, source
    ) -> None:
        # data-model §3.2, stated as a test because it is the invariant the whole
        # column exists to hold.
        url = f"{SUPPORT}boards/"
        await run(
            session, store, source, Fetcher({url: alpha_page("Boards")}), candidates=candidates(url)
        )

        await run(
            session,
            store,
            source,
            Fetcher({}, failures={url: TimeoutError("x")}),
            candidates=candidates(url),
        )
        assert store.records, "a failed re-crawl must not take the live content away"

        await run(
            session,
            store,
            source,
            Fetcher({url: alpha_page_v2("Boards")}),
            candidates=candidates(url),
        )
        assert store.records, "a changed page overwrites, it does not empty the document"

        document = await get_document(session, source.id, url)
        assert document is not None
        assert document.state == "indexed"
        assert document.vectors_live is True
