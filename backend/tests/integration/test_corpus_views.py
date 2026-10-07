"""Corpus views (T154): what the console must be able to show, proved against
the API it will read.

Each assertion here is a promise the console makes to an operator: a source
that lost every document still exists, a document's units are countable and
inspectable, a crawl job reports why it did less than it discovered, and the
counts in every view are live rows rather than remembered numbers.

The empty-corpus case is a test of its own rather than a fixture state that
other tests happen to pass through — an empty view that is only ever seen on
a fresh database is a view that has never been designed.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from tests.fixtures.database import describe_connection_failure, migrate_to_head, truncate_all

pytestmark = pytest.mark.integration

PRODUCT = "jira"


@pytest.fixture(scope="module")
def _schema() -> None:
    try:
        migrate_to_head()
    except Exception:  # noqa: BLE001
        pytest.skip(describe_connection_failure())


@pytest.fixture(autouse=True)
async def _database(_schema: None) -> AsyncIterator[None]:
    await truncate_all()
    yield
    await truncate_all()
    from app.db.session import dispose_engine

    await dispose_engine()


@pytest.fixture
async def client(_schema: None) -> AsyncIterator[AsyncClient]:
    from app.main import create_app, lifespan

    app = create_app(enable_probes=False)
    async with lifespan(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as http:
            yield http


async def _seed(
    *,
    documents: int = 1,
    state: str = "indexed",
    units_per_document: int = 3,
) -> dict:
    """A source with `documents` documents in `state`, each with real units."""
    from app.db.models import Document, DocumentUnit, Source
    from app.db.session import get_session_factory

    factory = get_session_factory()
    async with factory() as session:
        source = Source(
            name="Jira Cloud docs",
            start_url="https://support.atlassian.com/jira-cloud-administration/docs/",
            allowed_domains=["support.atlassian.com"],
            product_domain=PRODUCT,
        )
        session.add(source)
        await session.flush()
        created = []
        for index in range(documents):
            document = Document(
                source_id=source.id,
                url=f"{source.start_url}page-{index}",
                canonical_url=f"{source.start_url}page-{index}",
                title=f"Page {index}",
                product=PRODUCT,
                category="filters",
                page_type="documentation",
                language="en",
                state=state,
                state_detail=None,
                vectors_live=state == "indexed",
                content_fingerprint=f"fp-{index}",
            )
            session.add(document)
            await session.flush()
            for ordinal in range(units_per_document):
                session.add(
                    DocumentUnit(
                        document_id=document.id,
                        ordinal=ordinal,
                        vector_id=f"{document.id}#{ordinal:04d}",
                        heading_path=["Filters", f"Section {ordinal}"],
                        block_types=["paragraph"],
                        token_count=600 + ordinal,
                        overlap_tokens=50 if ordinal else 0,
                        text_fingerprint=f"u{index}-{ordinal}",
                    )
                )
            created.append(str(document.id))
        await session.commit()
        return {"source_id": str(source.id), "document_ids": created}


class TestSourceViews:
    async def test_a_source_reports_identity_product_counts_and_last_crawl(
        self, client: AsyncClient
    ) -> None:
        await _seed(documents=2)
        response = await client.get("/api/v1/sources")
        assert response.status_code == 200
        item = response.json()["items"][0]
        assert item["name"] == "Jira Cloud docs"
        assert item["product_domain"] == PRODUCT
        assert item["page_count"] == 2
        assert item["unit_count"] == 6
        # Never crawled is null, not "completed": a source that has never run is
        # a different report from one whose last crawl failed.
        assert item["last_crawl_at"] is None
        assert item["last_crawl_status"] is None

    async def test_a_source_whose_documents_are_all_deleted_stays_listed(
        self, client: AsyncClient
    ) -> None:
        """Edge case 16: the operator must still see the registration.

        A console that drops a source from its table the moment its last
        document is removed leaves the operator with a crawler running against
        something they can no longer see or disable.
        """
        from app.db.models import Document
        from app.db.session import get_session_factory

        seeded = await _seed(documents=1)
        factory = get_session_factory()
        async with factory() as session:
            document = await session.get(Document, uuid.UUID(seeded["document_ids"][0]))
            document.state = "deleted"
            document.vectors_live = False
            await session.commit()

        response = await client.get("/api/v1/sources")
        item = response.json()["items"][0]
        assert item["id"] == seeded["source_id"]
        assert item["page_count"] == 0, "a tombstoned document is not a live page"

    async def test_the_empty_corpus_is_an_empty_list_not_an_error(self, client: AsyncClient) -> None:
        for path in ("/api/v1/sources", "/api/v1/documents", "/api/v1/crawl-jobs"):
            response = await client.get(path)
            assert response.status_code == 200
            body = response.json()
            assert body["items"] == []
            assert body["total"] == 0


class TestDocumentViews:
    async def test_documents_expose_provenance_and_are_filterable(
        self, client: AsyncClient
    ) -> None:
        await _seed(documents=3)
        response = await client.get("/api/v1/documents?limit=10")
        assert response.status_code == 200
        item = response.json()["items"][0]
        for field in (
            "url",
            "title",
            "product",
            "category",
            "page_type",
            "language",
            "state",
            "content_fingerprint",
            "last_crawled_at",
            "indexed_at",
            "unit_count",
        ):
            assert field in item, f"the document view omits {field}"

        filtered = await client.get(f"/api/v1/documents?product={PRODUCT}&state=indexed")
        assert filtered.json()["total"] == 3
        missed = await client.get("/api/v1/documents?product=confluence")
        assert missed.json()["total"] == 0

    async def test_a_document_exposes_its_units_for_the_inspector(
        self, client: AsyncClient
    ) -> None:
        seeded = await _seed(documents=1, units_per_document=4)
        document_id = seeded["document_ids"][0]
        response = await client.get(f"/api/v1/documents/{document_id}/units")
        assert response.status_code == 200
        body = response.json()
        assert body["total"] == 4
        ordinals = [u["ordinal"] for u in body["items"]]
        assert ordinals == sorted(ordinals), "units are browsable in reconstruction order"
        unit = body["items"][0]
        assert unit["heading_path"] == ["Filters", "Section 0"]
        assert unit["token_count"] == 600
        assert "text" not in unit, "unit text is never served; the store holds it"

    async def test_a_missing_document_is_a_404(self, client: AsyncClient) -> None:
        response = await client.get(f"/api/v1/documents/{uuid.uuid4()}")
        assert response.status_code == 404


class TestCrawlViews:
    async def test_a_crawl_job_reports_counters_and_error_detail(
        self, client: AsyncClient
    ) -> None:
        from app.db.models import CrawlJob
        from app.db.session import get_session_factory

        seeded = await _seed(documents=1)
        factory = get_session_factory()
        async with factory() as session:
            job = CrawlJob(
                source_id=uuid.UUID(seeded["source_id"]),
                target_url="https://support.atlassian.com/jira-cloud-administration/docs/",
                status="failed",
                pages_discovered=5,
                pages_processed=2,
                pages_unchanged=1,
                pages_skipped=1,
                pages_failed=2,
                skipped_reasons={"robots_disallowed": 1},
                error_code="FETCH_FAILED",
                error_message="two pages returned 500",
            )
            session.add(job)
            await session.commit()

        response = await client.get("/api/v1/crawl-jobs")
        assert response.status_code == 200
        item = response.json()["items"][0]
        assert item["counters"]["discovered"] == 5
        assert item["counters"]["processed"] == 2
        assert item["error_code"] == "FETCH_FAILED"
        assert item["error_message"] == "two pages returned 500"
        assert item["skipped_reasons"] == {"robots_disallowed": 1}