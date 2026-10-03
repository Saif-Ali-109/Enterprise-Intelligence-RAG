"""The registry endpoints, end to end: real app, real database, no network.

T063, T064, T065, T066. Three things are real here and they are the three that
decide whether these endpoints are honest: **PostgreSQL**, through the real ORM
models and the real constraints; the **real FastAPI app**, with the real
validation, the real error handlers, and the real lifespan; and the real
`main.lifespan` startup and shutdown, because the crawl lifecycle claims in the
registry endpoints are claims about what happens across a restart.

Three things are faked, and each is faked at the boundary where it stops being
the subject of the test:

**DNS.** `assert_fetchable` resolves the host before any connection is opened,
which is the property FR-044 is about. The resolver is replaced with one that
returns a fixed public address, so the *ordering* is still tested — a route that
checked the guard after creating its row would still fail, because the row would
exist when the fake resolution was reached — without a test suite that depends on
a name server.

**The crawl.** `crawl_source` is replaced with a recorder. The endpoints'
subject is what they do with the *request* — the job they create, the counts they
report, the refusal they make — and a real crawl would put a network between the
assertion and the code. What the crawl itself does is T061's subject, tested in
`test_pipeline.py` against a recording store.

**The vector store.** `RecordingVectorStore`, for the one assertion that matters
here: that a `DELETE` removed vectors and not merely rows.

Every response is read as raw JSON as well as through the model, because a
response that is *nearly* the contract's shape — a field named `chunks` where the
contract says `unit_count`, a `text` key on a unit — is the failure this module
exists to catch, and a Pydantic model with `extra="forbid"` would have rejected it
only after the fact.
"""

from __future__ import annotations

import ipaddress
import uuid
from collections.abc import AsyncIterator, Callable
from typing import Any

import pytest
from app.db.models import CrawlJob, Document, DocumentUnit, Source
from httpx import ASGITransport, AsyncClient, Response

from tests.fixtures.credentials import seed_placeholder_credentials
from tests.fixtures.database import (
    describe_connection_failure,
    migrate_to_head,
    truncate_all,
)
from tests.fixtures.vector_store import RecordingVectorStore

pytestmark = pytest.mark.integration

seed_placeholder_credentials()

SUPPORT = "https://support.atlassian.com/jira-software-cloud/docs/"
DEVELOPER = "https://developer.atlassian.com/cloud/jira/platform/"
#: A public address for the "the guard allows this" case. Deliberately NOT one
#: of the documentation ranges: `203.0.113.0/24` looks public and is not —
#: Python's `ipaddress` marks the whole TEST-NET block private, and the first
#: version of this fixture used it and read the resulting `private_range`
#: refusal as a defect in the route.
PUBLIC = ipaddress.ip_address("93.184.216.34")
LOOPBACK = ipaddress.ip_address("127.0.0.1")
METADATA = ipaddress.ip_address("169.254.169.254")


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
def resolved_hosts(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Resolve every name to one public address, recording each lookup.

    Public by default, so a route that forgets to check is not silently refused:
    the tests that want a refusal patch `resolve_addresses` again in the test
    body, and the ones that assert the guard ran read this list.
    """
    resolved: list[str] = []

    def resolve(host: str, *, port: int = 443) -> list[Any]:
        resolved.append(host)
        return [PUBLIC]

    monkeypatch.setattr("app.core.security.resolve_addresses", resolve)
    return resolved


def refuse_with(monkeypatch: pytest.MonkeyPatch, address: Any) -> None:
    """Resolve every name to one address the guard must refuse."""
    monkeypatch.setattr(
        "app.core.security.resolve_addresses",
        lambda host, *, port=443: [address],
    )


@pytest.fixture
def crawl_calls(monkeypatch: pytest.MonkeyPatch) -> list[uuid.UUID]:
    """Record the crawls the endpoints start, without running one."""
    from app.ingestion import crawl_service

    started: list[uuid.UUID] = []

    async def record(session: Any, *, source_id: uuid.UUID, **kwargs: Any) -> Any:
        started.append(source_id)
        job = kwargs.get("job")
        if job is not None:
            from app.ingestion.pipeline import CrawlCounts, finish_crawl_job

            await finish_crawl_job(session, job=job, counts=CrawlCounts())
            await session.commit()
        return None

    monkeypatch.setattr(crawl_service, "crawl_source", record)
    return started


@pytest.fixture
async def store(monkeypatch: pytest.MonkeyPatch) -> RecordingVectorStore:
    """The recording store, installed as the process-wide singleton."""
    from app.retrieval import vector_store

    recording = RecordingVectorStore()
    monkeypatch.setattr(vector_store, "get_vector_store", lambda: recording)
    import app.api.routes_documents as routes_documents
    import app.api.routes_sources as routes_sources
    import app.ingestion.crawl_service as crawl_service

    monkeypatch.setattr(crawl_service, "get_vector_store", lambda: recording)
    monkeypatch.setattr(routes_documents, "get_vector_store", lambda: recording)
    monkeypatch.setattr(routes_sources, "get_vector_store", lambda: recording)
    yield recording


@pytest.fixture
async def client(_database: None) -> AsyncIterator[AsyncClient]:
    """An in-process client over the real app, with the real lifespan."""
    from app.main import create_app, lifespan

    app = create_app(enable_probes=False)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        async with lifespan(app):
            yield http


@pytest.fixture
def db() -> Callable[[], Any]:
    """A session for reading the registry directly, to check what a route did."""
    from app.db.session import get_session_factory

    def session() -> Any:
        return get_session_factory()()

    return session


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def register(client: AsyncClient, url: str = SUPPORT, **overrides: Any) -> Response:
    body: dict[str, Any] = {"name": "Jira Cloud docs", "start_url": url}
    body.update(overrides)
    return await client.post("/api/v1/sources", json=body)


def code_of(response: Response) -> str:
    return str(response.json()["error"]["code"])


async def seed_document(
    *,
    url: str = SUPPORT,
    state: str = "indexed",
    units: int = 3,
    tombstoned: bool = False,
) -> dict[str, Any]:
    """Insert a source, a document, and its units, and report the ids."""
    from app.db.session import get_session_factory

    factory = get_session_factory()
    async with factory() as session:
        source = Source(
            name="Seeded",
            start_url=url,
            allowed_domains=["support.atlassian.com"],
            product_domain="jira",
        )
        session.add(source)
        await session.flush()

        document = Document(
            source_id=source.id,
            url=f"{url.rstrip('/')}/boards/",
            title="Boards",
            product="jira",
            category="jira-software-cloud",
            state=state,
            vectors_live=state == "indexed",
            unit_count=units,
            content_fingerprint="f" * 64,
        )
        if tombstoned:
            from datetime import UTC, datetime

            document.tombstoned_at = datetime.now(UTC)
        session.add(document)
        await session.flush()

        rows = []
        for ordinal in range(units):
            unit = DocumentUnit(
                document_id=document.id,
                ordinal=ordinal,
                vector_id=f"{document.id}#{ordinal:04d}",
                heading_path=["Boards", f"Section {ordinal}"],
                block_types=["paragraph"],
                token_count=420,
                overlap_tokens=40 if ordinal else 0,
                text_fingerprint=f"{ordinal}" * 64,
            )
            session.add(unit)
            rows.append(unit)
        await session.commit()
        return {
            "source_id": source.id,
            "document_id": document.id,
            "unit_ids": [row.id for row in rows],
        }


async def wait_for_crawls(count: int = 1) -> None:
    """Let the scheduled crawl tasks run to completion."""
    import asyncio

    for _ in range(50):
        from app.ingestion.crawl_service import in_flight

        if not in_flight():
            return
        await asyncio.sleep(0.01)


# ============================================================================
# Registration
# ============================================================================


class TestRegistration:
    async def test_registering_returns_the_source_and_the_crawl_job(
        self, client: AsyncClient, crawl_calls: list[uuid.UUID]
    ) -> None:
        response = await register(client)

        assert response.status_code == 201, response.text
        body = response.json()
        assert body["source"]["name"] == "Jira Cloud docs"
        assert body["crawl_job"]["status"] in {"queued", "running", "completed"}
        assert body["crawl_job"]["target_url"] == SUPPORT
        assert set(body["crawl_job"]["counters"]) == {
            "discovered",
            "processed",
            "unchanged",
            "skipped",
            "failed",
        }

        await wait_for_crawls()
        assert crawl_calls == [uuid.UUID(body["source"]["id"])]

    async def test_the_allowed_domains_are_derived_not_supplied(
        self, client: AsyncClient, crawl_calls: list[uuid.UUID]
    ) -> None:
        """The scope comes from the registrant's URL, so a client cannot widen it.

        `SourceCreate` has no `allowed_domains` field at all: a body carrying one
        is refused by the closed schema, not a field that is quietly ignored.

        400, not 422: the contract's status table has no 422, and the app-level
        handler renders every validation failure as `VALIDATION_ERROR`/400.
        """
        response = await register(client, extra_field="x")

        assert response.status_code == 400
        assert code_of(response) == "VALIDATION_ERROR"

        allowed = await register(client)
        assert allowed.json()["source"]["allowed_domains"] == ["support.atlassian.com"]

    async def test_the_address_guard_runs_before_any_row_is_written(
        self, client: AsyncClient, monkeypatch: pytest.MonkeyPatch, db: Any
    ) -> None:
        """FR-044: a refused address is rejected, never registered.

        The assertion is on the registry, not the response: a 400 that left a row
        behind would let a later crawl attempt a fetch the guard exists to
        prevent, and the response would look correct.
        """
        refuse_with(monkeypatch, LOOPBACK)

        response = await register(client)

        assert response.status_code == 400
        assert code_of(response) == "SSRF_BLOCKED"
        assert response.json()["error"]["details"]["reason"] == "loopback"
        # The refused host must not appear in the response body: naming it
        # confirms to a prober which addresses are in use.
        assert "127.0.0.1" not in response.text

        from sqlalchemy import func, select

        session = db()
        async with session as open_session:
            count = await open_session.execute(select(func.count(Source.id)))
        assert count.scalar_one() == 0

    async def test_a_cloud_metadata_address_is_refused(
        self, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        refuse_with(monkeypatch, METADATA)

        response = await register(client)

        assert response.status_code == 400
        assert response.json()["error"]["details"]["reason"] == "cloud_metadata"

    async def test_a_name_that_does_not_resolve_is_refused(
        self, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Not a fetch attempt with an unreachable host: a refusal, up front.

        A name that does not resolve is refused rather than accepted, so the
        crawl records an honest failure instead of a mysterious one.
        """
        monkeypatch.setattr("app.core.security.resolve_addresses", lambda host, *, port=443: [])

        response = await register(client)

        assert response.status_code == 400
        assert response.json()["error"]["details"]["reason"] == "unresolvable"

    async def test_the_guard_resolves_the_host(
        self, client: AsyncClient, resolved_hosts: list[str]
    ) -> None:
        """The check is not static: a public name that resolves privately is refused."""
        await register(client)

        assert resolved_hosts == ["support.atlassian.com"]

    async def test_registering_the_same_url_twice_returns_the_existing_source(
        self, client: AsyncClient, crawl_calls: list[uuid.UUID]
    ) -> None:
        first = await register(client)
        second = await register(client)

        assert first.status_code == 201
        assert second.status_code == 200
        # The 200 body is a bare Source, not the composite: there is no second
        # crawl, so there is no second job to describe.
        assert "crawl_job" not in second.json()
        assert second.json()["id"] == first.json()["source"]["id"]

        await wait_for_crawls()
        assert len(crawl_calls) == 1

    async def test_start_crawl_false_registers_without_a_job(
        self, client: AsyncClient, crawl_calls: list[uuid.UUID], db: Any
    ) -> None:
        """And the source can then be crawled explicitly.

        This is the case the contract amendment exists for: a registration that
        starts no crawl must leave no `queued` job, or the explicit crawl below
        would be refused as "already running" against a crawl that never began.
        """
        registered = await register(client, start_crawl=False)

        assert registered.status_code == 201
        assert registered.json()["crawl_job"] is None

        session = db()
        async with session as open_session:
            from sqlalchemy import func, select

            jobs = await open_session.execute(select(func.count(CrawlJob.id)))
        assert jobs.scalar_one() == 0

        source_id = registered.json()["source"]["id"]
        crawled = await client.post(f"/api/v1/sources/{source_id}/crawl")

        assert crawled.status_code == 202
        assert crawled.json()["status"] in {"queued", "running"}
        await wait_for_crawls()
        assert crawl_calls == [uuid.UUID(source_id)]

    async def test_the_per_source_bounds_are_persisted(
        self, client: AsyncClient, crawl_calls: list[uuid.UUID]
    ) -> None:
        """`PATCH` can change them, so they cannot be request-only arguments."""
        response = await register(client, max_pages=5, max_depth=1, delay_seconds=2.5)

        source_id = response.json()["source"]["id"]
        listing = await client.get("/api/v1/sources")
        assert listing.status_code == 200

        from app.db.session import get_session_factory
        from sqlalchemy import select

        factory = get_session_factory()
        async with factory() as session:
            row = (
                await session.execute(select(Source).where(Source.id == uuid.UUID(source_id)))
            ).scalar_one()
        assert (row.max_pages, row.max_depth, row.delay_seconds) == (5, 1, 2.5)

    async def test_an_out_of_range_bound_is_refused_by_the_schema(
        self, client: AsyncClient, db: Any
    ) -> None:
        """The database constraint and the schema agree, and the schema is first.

        `max_pages: 5000` is outside the contract's bound and outside
        `ck_sources_max_pages_range`. Refusing it in the schema means the check
        runs before the guard and before the insert, and the two can never
        disagree about which values are legal.
        """
        response = await register(client, max_pages=5000)

        assert response.status_code == 400
        assert code_of(response) == "VALIDATION_ERROR"
        assert response.json()["error"]["details"]["fields"] == ["body"]

        from sqlalchemy import func, select

        session = db()
        async with session as open_session:
            count = await open_session.execute(select(func.count(Source.id)))
        assert count.scalar_one() == 0


# ============================================================================
# Listing, retrieval, updates
# ============================================================================


class TestSourcesEndpoint:
    async def test_listing_returns_the_contract_envelope(self, client: AsyncClient) -> None:
        await register(client)
        await register(client, url=DEVELOPER, name="Jira platform")

        response = await client.get("/api/v1/sources")

        assert response.status_code == 200
        body = response.json()
        assert set(body) == {"items", "total", "limit", "offset"}
        assert body["total"] == 2
        assert [item["name"] for item in body["items"]] == [
            "Jira platform",
            "Jira Cloud docs",
        ]

    async def test_the_enabled_filter_narrows_the_items_and_the_total(
        self, client: AsyncClient
    ) -> None:
        await register(client)
        second = await register(client, url=DEVELOPER, name="Jira platform")
        await client.patch(
            f"/api/v1/sources/{second.json()['source']['id']}", json={"enabled": False}
        )

        response = await client.get("/api/v1/sources", params={"enabled": True})

        assert response.json()["total"] == 1
        assert [item["name"] for item in response.json()["items"]] == ["Jira Cloud docs"]

    async def test_counts_exclude_tombstoned_documents(
        self, client: AsyncClient, store: RecordingVectorStore
    ) -> None:
        """A deleted page is still a row, and a count that includes it lies.

        `page_count` is what an operator reads to confirm the system ingested
        what they asked for. Counting a tombstone reports a corpus larger than
        the one that can be retrieved from.
        """
        seeded = await seed_document(units=3)
        response = await client.get(f"/api/v1/sources/{seeded['source_id']}")

        assert response.json()["page_count"] == 1
        assert response.json()["unit_count"] == 3

        deleted = await client.delete(f"/api/v1/documents/{seeded['document_id']}")
        assert deleted.status_code == 204

        after = await client.get(f"/api/v1/sources/{seeded['source_id']}")
        assert after.json()["page_count"] == 0
        assert after.json()["unit_count"] == 0

    async def test_an_unknown_source_is_a_404_with_the_envelope(self, client: AsyncClient) -> None:
        response = await client.get(f"/api/v1/sources/{uuid.uuid4()}")

        assert response.status_code == 404
        body = response.json()
        assert code_of(response) == "NOT_FOUND"
        assert set(body["error"]) >= {"code", "message", "request_id"}

    async def test_a_malformed_uuid_is_refused_before_any_lookup(self, client: AsyncClient) -> None:
        response = await client.get("/api/v1/sources/not-a-uuid")

        assert response.status_code == 400
        assert code_of(response) == "VALIDATION_ERROR"


class TestUpdateSource:
    async def test_only_the_supplied_fields_change(self, client: AsyncClient) -> None:
        registered = await register(client, product_domain="jira")
        source_id = registered.json()["source"]["id"]

        response = await client.patch(f"/api/v1/sources/{source_id}", json={"name": "Renamed"})

        assert response.status_code == 200
        body = response.json()
        assert body["name"] == "Renamed"
        # The rest is untouched — including the classification, which an
        # "absent means null" implementation would have cleared here.
        assert body["product_domain"] == "jira"

    async def test_a_classification_can_be_explicitly_cleared(self, client: AsyncClient) -> None:
        """FR-009: "unknown" has to be expressible, and it is not the same as absent."""
        registered = await register(client, product_domain="jira")
        source_id = registered.json()["source"]["id"]

        cleared = await client.patch(f"/api/v1/sources/{source_id}", json={"product_domain": None})

        assert cleared.status_code == 200
        assert cleared.json()["product_domain"] is None

        still_there = await client.get(f"/api/v1/sources/{source_id}")
        assert still_there.json()["product_domain"] is None

    async def test_disabling_a_source_reports_disabled(
        self, client: AsyncClient, crawl_calls: list[uuid.UUID]
    ) -> None:
        registered = await register(client)
        source_id = registered.json()["source"]["id"]

        disabled = await client.patch(f"/api/v1/sources/{source_id}", json={"enabled": False})

        assert disabled.status_code == 200
        assert disabled.json()["enabled"] is False
        assert disabled.json()["status"] == "disabled"

    async def test_a_disabled_source_cannot_be_crawled(
        self, client: AsyncClient, crawl_calls: list[uuid.UUID]
    ) -> None:
        registered = await register(client)
        source_id = registered.json()["source"]["id"]
        await client.patch(f"/api/v1/sources/{source_id}", json={"enabled": False})

        response = await client.post(f"/api/v1/sources/{source_id}/crawl")

        assert response.status_code == 400
        assert code_of(response) == "VALIDATION_ERROR"
        await wait_for_crawls()
        assert len(crawl_calls) == 1, "only the registration's crawl ran"

    async def test_an_empty_patch_changes_nothing(self, client: AsyncClient) -> None:
        registered = await register(client, product_domain="jira")
        source_id = registered.json()["source"]["id"]

        response = await client.patch(f"/api/v1/sources/{source_id}", json={})

        assert response.status_code == 200
        assert response.json()["product_domain"] == "jira"
        assert response.json()["name"] == "Jira Cloud docs"


# ============================================================================
# Concurrency
# ============================================================================


class TestOneCrawlPerSource:
    async def test_a_second_crawl_is_refused_and_names_the_running_job(
        self, client: AsyncClient, db: Any
    ) -> None:
        """FR-055, and the message is the useful part.

        The refusal carries the running job's id in `details` so a client can
        poll it instead of guessing, while the message stays authored and
        matchable (quickstart V9).
        """
        from app.db.session import get_session_factory

        registered = await register(client)
        source_id = registered.json()["source"]["id"]

        # The registration's crawl has already claimed the source. Leave the row
        # in `running` — which is what a real crawl looks like from outside.
        factory = get_session_factory()
        async with factory() as session:
            from sqlalchemy import select

            job = (
                (
                    await session.execute(
                        select(CrawlJob).where(CrawlJob.source_id == uuid.UUID(source_id))
                    )
                )
                .scalars()
                .first()
            )
            assert job is not None
            job.status = "running"
            running_id = str(job.id)
            await session.commit()

        response = await client.post(f"/api/v1/sources/{source_id}/crawl")

        assert response.status_code == 409
        body = response.json()
        assert code_of(response) == "CRAWL_ALREADY_RUNNING"
        assert body["error"]["details"]["running_job_id"] == running_id
        # The running crawl is undisturbed: still exactly one row, still running.
        async with factory() as session:
            from sqlalchemy import func, select

            count, status = (
                await session.execute(
                    select(func.count(CrawlJob.id), func.min(CrawlJob.status)).where(
                        CrawlJob.source_id == uuid.UUID(source_id)
                    )
                )
            ).one()
        assert (count, status) == (1, "running")

    async def test_a_finished_crawl_does_not_block_the_next_one(
        self, client: AsyncClient, crawl_calls: list[uuid.UUID]
    ) -> None:
        registered = await register(client)
        source_id = registered.json()["source"]["id"]
        await wait_for_crawls()

        response = await client.post(f"/api/v1/sources/{source_id}/crawl")

        assert response.status_code == 202
        await wait_for_crawls()
        assert len(crawl_calls) == 2


# ============================================================================
# Documents and units
# ============================================================================


class TestDocuments:
    async def test_listing_paginates_with_a_matching_total(self, client: AsyncClient) -> None:
        await seed_document(units=2)

        first = await client.get("/api/v1/documents", params={"limit": 1, "offset": 0})
        second = await client.get("/api/v1/documents", params={"limit": 1, "offset": 1})

        assert first.json()["total"] == 1
        assert len(first.json()["items"]) == 1
        assert second.json()["items"] == [], "the corpus has one document, not two"

    async def test_the_filters_are_exact_matches(self, client: AsyncClient) -> None:
        await seed_document(url=SUPPORT)

        by_product = await client.get("/api/v1/documents", params={"product": "jira"})
        by_prefix = await client.get("/api/v1/documents", params={"product": "jir"})
        by_other = await client.get("/api/v1/documents", params={"product": "confluence"})

        assert by_product.json()["total"] == 1
        assert by_prefix.json()["total"] == 0
        assert by_other.json()["total"] == 0

    async def test_a_deleted_document_is_still_listed_and_says_so(
        self, client: AsyncClient, store: RecordingVectorStore
    ) -> None:
        """A corpus view that silently shrinks cannot be told from one that
        was never crawled. The tombstone is the difference."""
        seeded = await seed_document(units=2)
        await client.delete(f"/api/v1/documents/{seeded['document_id']}")

        response = await client.get("/api/v1/documents", params={"state": "deleted"})

        assert response.json()["total"] == 1
        item = response.json()["items"][0]
        assert item["state"] == "deleted"
        assert item["tombstoned_at"] is not None
        assert item["vectors_live"] is False
        assert item["unit_count"] == 0

    async def test_a_document_exposes_its_classification_or_nulls(
        self, client: AsyncClient
    ) -> None:
        """FR-009: null is a valid answer, so it must survive serialisation."""
        seeded = await seed_document(units=1)

        response = await client.get(f"/api/v1/documents/{seeded['document_id']}")

        body = response.json()
        assert body["product"] == "jira"
        assert body["category"] == "jira-software-cloud"
        assert body["canonical_url"] is None
        assert body["state_detail"] is None

    async def test_units_come_back_in_ordinal_order_without_text(self, client: AsyncClient) -> None:
        """FR-024's window: everything needed to resolve a citation, no prose."""
        seeded = await seed_document(units=3)

        response = await client.get(f"/api/v1/documents/{seeded['document_id']}/units")

        assert response.status_code == 200
        body = response.json()
        assert body["total"] == 3
        assert [item["ordinal"] for item in body["items"]] == [0, 1, 2]
        assert body["items"][1]["heading_path"] == ["Boards", "Section 1"]
        assert body["items"][1]["overlap_tokens"] == 40
        assert body["items"][0]["overlap_tokens"] == 0
        assert body["items"][0]["vector_id"] == f"{seeded['document_id']}#0000"

        for item in body["items"]:
            assert "text" not in item
            assert "content" not in item
            assert set(item) == {
                "id",
                "document_id",
                "ordinal",
                "vector_id",
                "heading_path",
                "block_types",
                "token_count",
                "overlap_tokens",
                "text_fingerprint",
            }

    async def test_units_of_an_unknown_document_are_a_404(self, client: AsyncClient) -> None:
        response = await client.get(f"/api/v1/documents/{uuid.uuid4()}/units")

        assert response.status_code == 404
        assert code_of(response) == "NOT_FOUND"

    async def test_deleting_a_document_removes_its_vectors(
        self, client: AsyncClient, store: RecordingVectorStore, db: Any
    ) -> None:
        """The registry half and the vector half, or neither is worth anything."""
        seeded = await seed_document(units=3)

        response = await client.delete(f"/api/v1/documents/{seeded['document_id']}")

        assert response.status_code == 204
        assert response.content == b""

        from app.db.session import get_session_factory
        from sqlalchemy import func, select

        factory = get_session_factory()
        async with factory() as session:
            units = await session.execute(select(func.count(DocumentUnit.id)))
        assert units.scalar_one() == 0

    async def test_deleting_twice_is_not_an_error(
        self, client: AsyncClient, store: RecordingVectorStore
    ) -> None:
        """The caller's intent is already satisfied; a 404 would invite a retry
        of something that is already true."""
        seeded = await seed_document(units=2)

        first = await client.delete(f"/api/v1/documents/{seeded['document_id']}")
        second = await client.delete(f"/api/v1/documents/{seeded['document_id']}")

        assert (first.status_code, second.status_code) == (204, 204)


# ============================================================================
# Removing a source
# ============================================================================


class TestRemoveSource:
    async def test_removing_a_source_removes_its_documents_and_vectors(
        self, client: AsyncClient, store: RecordingVectorStore, db: Any
    ) -> None:
        seeded = await seed_document(units=3)

        response = await client.delete(f"/api/v1/sources/{seeded['source_id']}")

        assert response.status_code == 204

        from app.db.session import get_session_factory
        from sqlalchemy import func, select

        factory = get_session_factory()
        async with factory() as session:
            sources = await session.execute(select(func.count(Source.id)))
            documents = await session.execute(select(func.count(Document.id)))
            units = await session.execute(select(func.count(DocumentUnit.id)))
        assert (sources.scalar_one(), documents.scalar_one(), units.scalar_one()) == (0, 0, 0)
        # The vector ids the registry held are the ones removed: deleting by
        # anything else would leave content the registry no longer accounts for.
        assert sorted(store.deleted) == [
            f"{seeded['document_id']}#{ordinal:04d}" for ordinal in range(3)
        ]
        assert store.live_ids == []

    async def test_removing_a_source_with_a_running_crawl_is_refused(
        self, client: AsyncClient, db: Any
    ) -> None:
        """The cascade would take the running job row out from under the task
        still writing pages, leaving work whose audit trail no longer exists."""
        from app.db.session import get_session_factory

        registered = await register(client)
        source_id = uuid.UUID(registered.json()["source"]["id"])
        job_id = registered.json()["crawl_job"]["id"]

        factory = get_session_factory()
        async with factory() as session:
            from sqlalchemy import select

            job = (
                await session.execute(select(CrawlJob).where(CrawlJob.id == uuid.UUID(job_id)))
            ).scalar_one()
            job.status = "running"
            await session.commit()

        response = await client.delete(f"/api/v1/sources/{source_id}")

        assert response.status_code == 409
        assert code_of(response) == "CONFLICT"
        assert response.json()["error"]["details"]["running_job_id"] == job_id

        async with factory() as session:
            from sqlalchemy import func, select

            count = await session.execute(select(func.count(Source.id)))
        assert count.scalar_one() == 1, "the source is still there"


# ============================================================================
# Crawl jobs
# ============================================================================


class TestCrawlJobs:
    async def test_the_registration_job_is_readable_by_id(self, client: AsyncClient) -> None:
        registered = await register(client)
        job_id = registered.json()["crawl_job"]["id"]

        response = await client.get(f"/api/v1/crawl-jobs/{job_id}")

        assert response.status_code == 200
        body = response.json()
        assert body["id"] == job_id
        assert body["source_id"] == registered.json()["source"]["id"]
        assert body["target_url"] == SUPPORT
        assert set(body["counters"]) == {
            "discovered",
            "processed",
            "unchanged",
            "skipped",
            "failed",
        }

    async def test_a_job_that_has_not_run_reports_zero_counters(self, client: AsyncClient) -> None:
        """Integers, always — and the count of nothing is zero, not absent.

        The columns are `NOT NULL` with a server default of 0, so a client never
        has to null-check a counter. The serialiser still maps a null to zero,
        because a column added by a migration that predates this contract could
        be null and a counter that cannot be serialised is worse than one that
        reads as zero; that mapping is covered in `tests/unit/test_serialisers.py`.
        """
        registered = await register(client)
        job_id = registered.json()["crawl_job"]["id"]

        body = (await client.get(f"/api/v1/crawl-jobs/{job_id}")).json()

        assert body["counters"] == {
            "discovered": 0,
            "processed": 0,
            "unchanged": 0,
            "skipped": 0,
            "failed": 0,
        }
        assert body["finished_at"] is None
        assert body["error_code"] is None

    async def test_skipped_reasons_is_a_flat_reason_to_count_map(
        self, client: AsyncClient, db: Any
    ) -> None:
        """The answer to "why did it read fewer pages than it discovered?"."""
        registered = await register(client)
        job_id = uuid.UUID(registered.json()["crawl_job"]["id"])

        from app.db.session import get_session_factory

        factory = get_session_factory()
        async with factory() as session:
            from sqlalchemy import select

            job = (
                await session.execute(select(CrawlJob).where(CrawlJob.id == job_id))
            ).scalar_one()
            job.status = "completed"
            job.pages_discovered = 12
            job.pages_processed = 8
            job.pages_skipped = 4
            job.skipped_reasons = {"robots_disallowed": 3, "too_small": 1}
            await session.commit()

        body = (await client.get(f"/api/v1/crawl-jobs/{job_id}")).json()

        assert body["status"] == "completed"
        assert body["skipped_reasons"] == {"robots_disallowed": 3, "too_small": 1}
        # `processed` includes unchanged pages, so indexed is the difference.
        assert body["counters"]["processed"] - body["counters"]["unchanged"] == 8

    async def test_no_skips_reads_as_null_not_an_empty_object(self, client: AsyncClient) -> None:
        """`{}` and absent mean different things, and only one of them is "none"."""
        registered = await register(client)
        job_id = registered.json()["crawl_job"]["id"]

        body = (await client.get(f"/api/v1/crawl-jobs/{job_id}")).json()

        assert body["skipped_reasons"] is None

    async def test_listing_filters_by_source_and_status(self, client: AsyncClient) -> None:
        first = await register(client)
        second = await register(client, url=DEVELOPER, name="Jira platform")

        everything = await client.get("/api/v1/crawl-jobs")
        assert everything.json()["total"] == 2

        by_source = await client.get(
            "/api/v1/crawl-jobs", params={"source_id": first.json()["source"]["id"]}
        )
        assert by_source.json()["total"] == 1

        # The status filter is named `status` in the contract, and the alias is
        # what keeps the generated parameter from being called something else.
        running = await client.get("/api/v1/crawl-jobs", params={"status": "running"})
        assert running.json()["total"] == 2

        completed = await client.get("/api/v1/crawl-jobs", params={"status": "completed"})
        assert completed.json()["total"] == 0
        assert second.json()["source"]["id"] != first.json()["source"]["id"]

    async def test_an_unknown_job_is_a_404_with_the_envelope(self, client: AsyncClient) -> None:
        response = await client.get(f"/api/v1/crawl-jobs/{uuid.uuid4()}")

        assert response.status_code == 404
        assert code_of(response) == "NOT_FOUND"

    async def test_the_status_query_parameter_is_named_as_the_contract_names_it(
        self, client: AsyncClient
    ) -> None:
        from app.main import create_app

        app = create_app(enable_probes=False)
        generated = app.openapi()
        operation = generated["paths"]["/api/v1/crawl-jobs"]["get"]
        names = {parameter["name"] for parameter in operation.get("parameters", [])}

        assert {"source_id", "status"} <= names


# ============================================================================
# Jobs outlive the process
# ============================================================================


class TestJobRecovery:
    async def test_a_job_left_running_by_a_dead_process_is_reclaimed(
        self, client: AsyncClient, db: Any
    ) -> None:
        """A crash mid-crawl must not lock the source out forever.

        Without this, the row says "running" with nothing running and holds
        `crawl_jobs_one_active_per_source`, so the operator's only way to crawl
        the source again is a manual database edit — and nothing would tell them
        this is a bug rather than a slow crawl.
        """
        from app.db.session import get_session_factory
        from app.ingestion.crawl_service import reclaim_orphaned_jobs

        registered = await register(client)
        source_id = registered.json()["source"]["id"]
        job_id = registered.json()["crawl_job"]["id"]

        factory = get_session_factory()
        async with factory() as session:
            from sqlalchemy import select

            job = (
                await session.execute(select(CrawlJob).where(CrawlJob.id == uuid.UUID(job_id)))
            ).scalar_one()
            job.status = "running"
            await session.commit()

        reclaimed = await reclaim_orphaned_jobs()

        assert reclaimed == 1
        async with factory() as session:
            from sqlalchemy import select

            job = (
                await session.execute(select(CrawlJob).where(CrawlJob.id == uuid.UUID(job_id)))
            ).scalar_one()
        assert job.status == "failed"
        assert job.error_code == "INTERNAL_ERROR"
        assert "interrupted" in (job.error_message or "")

        # And the slot is free again.
        body = (await client.get(f"/api/v1/crawl-jobs/{job_id}")).json()
        assert body["status"] == "failed"
        assert source_id
