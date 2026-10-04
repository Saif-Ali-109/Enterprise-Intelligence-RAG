"""Dense retrieval's hydration contract (T082, FR-004, FR-020).

`retrieve()` joins three sources: the search hit (identity and similarity), the
fetched record (text and metadata), and the registry (provenance of record). This
file pins *which source answers which question*, because the answer changed for
one of them after a live measurement and no unit test could see it: a double that
injects hit metadata makes every assertion pass, while the live service returns
hits whose metadata is always empty.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

import pytest

pytestmark = pytest.mark.unit


@dataclass
class _FakeStore:
    """A store double whose hit metadata is empty on purpose — like the real one."""

    hits: list[Any]
    records: list[Any]
    searches: list[dict[str, Any]] = field(default_factory=list)
    fetches: list[list[str]] = field(default_factory=list)

    async def search(self, *, query, top_k, namespace=None, metadata_filter=None):
        self.searches.append(
            {
                "query": query,
                "top_k": top_k,
                "namespace": namespace,
                "metadata_filter": metadata_filter,
            }
        )
        return self.hits

    async def fetch(self, ids, *, namespace=None):
        self.fetches.append(list(ids))
        return [r for r in self.records if r.id in set(ids)]


class _NoSession:
    """Returns `(unit, document)` rows, as the registry join returns them."""

    def __init__(self, rows):
        self._rows = rows

    async def execute(self, _statement):
        return self

    def all(self):
        return iter(self._rows)


def _record(vector_id: str, *, url: str = "https://support.atlassian.com/x"):
    from app.retrieval.vector_store import VectorRecord

    return VectorRecord(
        id=vector_id,
        text="To rotate a token, create a new one and revoke the previous.",
        metadata={
            "document_id": "doc-1",
            "source_url": url,
            "title": "Manage API tokens",
            "product": "jira",
            "category": "rest-api",
            "page_type": "api_reference",
        },
    )


def _unit_row(
    vector_id: str,
    document_id,
    *,
    url: str = "https://support.atlassian.com/x",
    product: str | None = "jira",
    category: str | None = "rest-api",
    page_type: str | None = "api_reference",
    title: str | None = "Manage API tokens",
):
    from app.db.models import Document, DocumentUnit

    unit = DocumentUnit(
        id=uuid4(),
        document_id=document_id,
        vector_id=vector_id,
        heading_path=["Manage API tokens", "Rotate a token"],
        token_count=220,
        ordinal=4,
        text_fingerprint="f" * 64,
    )
    document = Document(
        id=document_id,
        source_id=uuid4(),
        url=url,
        canonical_url=url,
        title=title,
        product=product,
        category=category,
        page_type=page_type,
        language="en",
    )
    return unit, document


class TestHydrationSource:
    async def test_provenance_comes_from_the_registry_not_the_index(self) -> None:
        """Provenance is joined, not read out of a search hit.

        Measured against the live index on 2026-10-04, on `pinecone==10.0.0`:
        `Index.search` returns hits whose `metadata` is `None` for every `fields`
        value tried, and `Index.fetch` answered empty for freshly written ids
        long after `search` had them. A retriever reading either one passes every
        unit test whose double populates it and returns nothing live.
        """
        from app.retrieval.retriever import retrieve
        from app.retrieval.vector_store import SearchHit

        vector_id = "11111111-1111-1111-1111-111111111111#0004"
        document_id = uuid4()
        unit, _document = _unit_row(vector_id, document_id)
        hit = SearchHit(id=vector_id, score=0.51, metadata={})  # empty, like the real service
        store = _FakeStore(hits=[hit], records=[_record(vector_id)])
        session = _NoSession([(unit, _document)])

        candidates = await retrieve("how do I rotate an api token", session=session, store=store)

        assert len(candidates) == 1, "a hit the registry can vouch for was dropped"
        candidate = candidates[0]
        assert candidate.source_url == "https://support.atlassian.com/x"
        assert candidate.title == "Manage API tokens"
        assert candidate.product == "jira"
        assert candidate.category == "rest-api"
        assert candidate.page_type == "api_reference"
        assert candidate.heading_path == ["Manage API tokens", "Rotate a token"]
        assert candidate.retrieval_score == 0.51
        assert candidate.text.startswith("To rotate a token")
        assert candidate.unit_id == unit.id

    async def test_a_record_that_did_not_arrive_yet_still_yields_a_candidate(self) -> None:
        """A late `fetch` must not empty the pool.

        The index acknowledged the upsert and `search` returns the hit, but
        `fetch` can still answer empty. The unit's text is what the fetch was
        for, and a textless candidate is refused by the reranker — a visible
        refusal at the stage that can explain it, rather than a silent empty
        pool here that reads as "nothing indexed".
        """
        from app.retrieval.retriever import retrieve
        from app.retrieval.vector_store import SearchHit

        vector_id = "11111111-1111-1111-1111-111111111111#0004"
        store = _FakeStore(hits=[SearchHit(id=vector_id, score=0.5, metadata={})], records=[])
        session = _NoSession([_unit_row(vector_id, uuid4())])

        candidates = await retrieve("q", session=session, store=store)

        assert len(candidates) == 1
        assert candidates[0].text == ""
        assert candidates[0].source_url

    async def test_the_registry_outranks_the_index_for_the_link(self) -> None:
        """The citation's url is the registry's, whatever the record claims."""
        from app.retrieval.retriever import retrieve
        from app.retrieval.vector_store import SearchHit

        vector_id = "11111111-1111-1111-1111-111111111111#0004"
        store = _FakeStore(
            hits=[SearchHit(id=vector_id, score=0.5, metadata={})],
            records=[_record(vector_id, url="https://stale.example/page")],
        )
        session = _NoSession([_unit_row(vector_id, uuid4())])

        candidates = await retrieve("q", session=session, store=store)

        assert candidates[0].source_url == "https://support.atlassian.com/x"

    async def test_a_hit_the_registry_does_not_vouch_for_is_dropped(self) -> None:
        from app.retrieval.retriever import retrieve
        from app.retrieval.vector_store import SearchHit

        vector_id = "11111111-1111-1111-1111-111111111111#0004"
        hit = SearchHit(id=vector_id, score=0.5, metadata={})
        store = _FakeStore(hits=[hit], records=[_record(vector_id)])

        assert await retrieve("q", session=_NoSession([]), store=store) == []

    async def test_a_document_the_registry_cannot_name_a_page_for_is_dropped(self) -> None:
        """Unlinkable evidence cannot become a citation, so it never enters the pool."""
        from app.retrieval.retriever import retrieve
        from app.retrieval.vector_store import SearchHit

        vector_id = "11111111-1111-1111-1111-111111111111#0004"
        store = _FakeStore(hits=[SearchHit(id=vector_id, score=0.4, metadata={})], records=[])
        session = _NoSession([_unit_row(vector_id, uuid4(), url="")])

        assert await retrieve("q", session=session, store=store) == []


class TestRequestShape:
    async def test_the_pool_is_configuration_not_a_constant(self) -> None:
        from app.core.config import get_settings
        from app.retrieval.retriever import retrieve

        store = _FakeStore(hits=[], records=[])
        await retrieve("q", session=_NoSession([]), store=store)

        assert store.searches[0]["top_k"] == get_settings().retrieval_candidate_pool

    async def test_the_namespace_is_configuration_too(self) -> None:
        from app.core.config import get_settings
        from app.retrieval.retriever import retrieve

        store = _FakeStore(hits=[], records=[])
        await retrieve("q", session=_NoSession([]), store=store)

        assert store.searches[0]["namespace"] == get_settings().pinecone_namespace
