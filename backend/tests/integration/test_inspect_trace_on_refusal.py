"""`inspect: true` must answer "where did it stop?" — on a refusal too (FR-050).

Measured 2026-10-13. Asking "How do I save a filter in Jira Cloud so I can run it
again later?" with `inspect: true` returned a refusal whose `trace.retrieval` and
`trace.reranking` were both **empty objects**, while the same response's
`searched` block reported 12 candidates retrieved, 6 reranked and 5 evidence units
selected. An operator reading that trace would conclude retrieval found nothing.

The cause was placement, not logic: the four trace artifacts were populated at the
end of the *answered* path, after every refusal return, so no refusal could ever
carry them. The pre-generation refusal branch did write a trace — and wrote
`"candidates": []` unconditionally, which is the same wrong answer stated more
confidently.

These tests pin the fixed behaviour: a refusal's trace carries the candidates that
were actually retrieved and reranked, and no unit text ever appears in it.
"""

from __future__ import annotations

from typing import Any

import pytest
from app.chat.service import run_chat

from tests.fixtures.credentials import seed_placeholder_credentials
from tests.fixtures.database import (
    describe_connection_failure,
    migrate_to_head,
    truncate_all,
)

pytestmark = [pytest.mark.integration]

seed_placeholder_credentials()

QUESTION = "How do I save a filter in Jira Cloud so I can run it again later?"
UNIT_TEXT = (
    "To save a filter in Jira Cloud, open the filter manager and choose Save as filter. "
    "A saved filter can be run again by name from the same screen."
)


@pytest.fixture(scope="module")
def _schema() -> None:
    try:
        migrate_to_head()
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"{describe_connection_failure()}: {exc}")


@pytest.fixture(autouse=True)
async def _database(_schema: None) -> Any:
    await truncate_all()
    yield
    await truncate_all()
    from app.db.session import dispose_engine

    await dispose_engine()


class _Store:
    """One hit, with text. Provenance is joined from the registry, as in production."""

    def __init__(self, vector_id: str) -> None:
        self._vector_id = vector_id

    async def search(self, **_kwargs: Any) -> list[Any]:
        from app.retrieval.vector_store import SearchHit

        return [SearchHit(id=self._vector_id, score=0.81)]

    async def fetch(self, ids: list[str], **_kwargs: Any) -> list[Any]:
        from app.retrieval.vector_store import VectorRecord

        return [VectorRecord(id=i, text=UNIT_TEXT) for i in ids if i == self._vector_id]


class _RefusingProvider:
    """Classifies confidently, then answers "not answerable".

    That is the path under test: the pipeline retrieves, reranks and selects
    evidence successfully, and the *generation* stage is where it stops.
    """

    async def classify(self, **kwargs: Any) -> dict[str, Any]:
        system = str(kwargs.get("system", ""))
        if "answer verifier" in system:
            return {"classification": "UNSUPPORTED", "reason": "scripted refusal"}
        return {
            "detected_product": "jira",
            "detected_category": "user-documentation",
            "intent": "how_to",
            "entities": [],
            "confidence": {"product": 0.95, "category": 0.9, "intent": 0.9, "entities": 0.5},
        }

    async def complete(self, **_kwargs: Any) -> Any:
        import json

        from app.generation.provider import Completion

        return Completion(
            text=json.dumps({"answerable": True, "answer": "Save it. [1]", "citations": []}),
            model="scripted",
        )


async def _seed(session: Any) -> str:
    import uuid

    from app.db.models import Document, DocumentUnit, Source

    source_id = uuid.uuid4()
    document_id = uuid.uuid4()
    vector_id = f"{document_id}#0000"
    session.add(
        Source(
            id=source_id,
            name="Jira Cloud administration docs",
            start_url="https://example.invalid/",
            allowed_domains=["example.invalid"],
        )
    )
    session.add(
        Document(
            id=document_id,
            source_id=source_id,
            url="https://example.invalid/save-your-search-as-a-filter",
            title="Save your search as a filter",
            product="jira",
            category="user-documentation",
            page_type="documentation",
            language="en",
            state="indexed",
            vectors_live=True,
            unit_count=1,
            content_fingerprint="fp",
        )
    )
    session.add(
        DocumentUnit(
            id=uuid.uuid4(),
            document_id=document_id,
            ordinal=0,
            vector_id=vector_id,
            text_fingerprint="ufp",
            heading_path=["Save your search as a filter"],
            block_types=["heading", "paragraph"],
            token_count=612,
            overlap_tokens=0,
        )
    )
    await session.commit()
    return vector_id


@pytest.fixture
async def session() -> Any:
    from app.db.session import get_session_factory

    factory = get_session_factory()
    async with factory() as db:
        yield db


async def _ask(session: Any, *, inspect: bool) -> dict[str, Any]:
    vector_id = await _seed(session)
    result = await run_chat(
        QUESTION,
        session=session,
        store=_Store(vector_id),
        provider=_RefusingProvider(),
        inspect=inspect,
    )
    return dict(result.payload)


async def test_a_refusal_trace_carries_the_candidates_that_were_retrieved(
    session: Any,
) -> None:
    """The regression, stated as the thing an operator would otherwise believe.

    Before this fix the same request returned `trace.retrieval == {}` while
    `searched.candidates_retrieved` was 1. A trace that disagrees with the
    response beside it is worse than no trace, because it is believed.
    """
    payload = await _ask(session, inspect=True)

    assert payload["outcome"] == "refused"
    searched = payload["searched"]
    trace = payload["trace"]
    assert trace, "a refusal with inspect: true must carry a trace"

    retrieval = trace.get("retrieval") or {}
    assert retrieval.get("candidates"), (
        "the trace claims retrieval found nothing while the same response reports "
        f"{searched['candidates_retrieved']} candidates"
    )
    assert len(retrieval["candidates"]) == searched["candidates_retrieved"]
    assert retrieval["candidates"][0]["heading_path"] == ["Save your search as a filter"]
    assert retrieval["candidates"][0]["product"] == "jira"

    # `applied_filters` is empty here, and correctly so: a single candidate is below
    # the evidence minimum, so the widening path replaced the filter with a wide
    # search. The two facts corroborate each other, which is the point of recording
    # both — an empty filter next to `widened: false` would be a filter that
    # matched nothing, and a different story entirely.
    assert "applied_filters" in retrieval
    assert isinstance(retrieval.get("widened"), bool)
    if not retrieval["applied_filters"]:
        assert retrieval["widened"] is True, (
            "an empty filter that was not widened would mean the filter matched "
            "nothing, which is a different failure"
        )

    reranking = trace.get("reranking") or {}
    assert reranking.get("submitted") == searched["candidates_retrieved"]
    assert len(reranking.get("candidates") or []) == searched["candidates_reranked"]


async def test_the_trace_records_where_generation_stopped(session: Any) -> None:
    """Generation is the stage that refused, so it is the stage the trace names."""
    payload = await _ask(session, inspect=True)

    trace = payload["trace"]
    assert trace.get("generation"), "the refusal came from generation; the trace should say so"
    assert (trace.get("citations") or {}).get("granularity") in {"none", "list", "per_claim"}


async def test_no_unit_text_reaches_the_trace(session: Any) -> None:
    """The trace persists into `query_logs`, so it must carry no corpus text."""
    payload = await _ask(session, inspect=True)

    serialised = str(payload["trace"])
    assert "Save as filter" not in serialised
    assert "filter manager" not in serialised.lower()
    for stage in ("retrieval", "reranking", "generation", "citations"):
        blob = str((payload["trace"] or {}).get(stage) or "")
        assert UNIT_TEXT[:40] not in blob


async def test_without_inspect_there_is_no_trace(session: Any) -> None:
    """The trace is opt-in, and stays opt-in after the move."""
    payload = await _ask(session, inspect=False)

    assert payload.get("trace") is None
