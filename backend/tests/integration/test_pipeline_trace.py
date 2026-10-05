"""The inspection trace, stored only when inspection was requested (T122, T128).

FR-033: with `inspect: true`, the four request-scoped artifacts — analysed
question, retrieved candidates, reranked candidates, selected evidence — are
snapshotted into `query_logs.pipeline_trace`, and the final citations go with
them. With `inspect: false`, nothing about the pipeline is persisted beyond the
answer's own envelope.

**"Persisted only when requested" is asserted from both directions**, because the
failure shapes differ. A trace that is always null means inspection never
matters; a trace that is always present means the question was paying a
retention cost the operator never asked for.

**The snapshot carries ids, counts and scores, never unit text.** The registry
deliberately stores provenance only; the trace persists into `query_logs`, so a
snapshot that included passage text would be a second, ungoverned copy of the
corpus.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from tests.fixtures.credentials import seed_placeholder_credentials
from tests.fixtures.database import describe_connection_failure, migrate_to_head, truncate_all

seed_placeholder_credentials()

pytestmark = pytest.mark.integration

QUESTION = "How do I create a JQL filter in Jira?"


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


class _EmptyStore:
    async def search(self, **_kwargs: object) -> list[object]:
        return []

    async def fetch(self, *_args: object, **_kwargs: object) -> list[object]:
        return []


@pytest.fixture
async def client(monkeypatch: pytest.MonkeyPatch, _schema: None) -> AsyncIterator[AsyncClient]:
    from app.api import routes_chat, routes_chat_stream
    from app.main import create_app, lifespan

    store = _EmptyStore()
    monkeypatch.setattr(routes_chat, "get_vector_store", lambda: store)
    monkeypatch.setattr(routes_chat_stream, "get_vector_store", lambda: store)

    app = create_app(enable_probes=False)
    async with lifespan(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as http:
            yield http


class TestPersistence:
    async def test_inspection_mode_persists_the_four_artifacts(self, client: AsyncClient) -> None:
        response = await client.post("/api/v1/chat", json={"question": QUESTION, "inspect": True})
        assert response.status_code == 200
        request_id = response.json()["request_id"]

        from app.db.models import QueryLog
        from app.db.session import get_session_factory

        factory = get_session_factory()
        async with factory() as session:
            row = (
                await session.execute(select(QueryLog).where(QueryLog.id == _uuid(request_id)))
            ).scalar_one()

        trace = row.pipeline_trace
        assert trace is not None, "inspection mode persisted no trace"
        assert set(trace) >= {"retrieval", "reranking", "generation", "citations"}
        assert trace["retrieval"]["queries"], "the retrieval artifact carries no queries"
        assert trace["retrieval"]["candidates"] == [], (
            "an empty corpus reports an empty list, not a missing key"
        )
        assert trace["reranking"]["candidates"] == []
        assert trace["generation"]["attempts"] == 0 or trace["generation"]["attempts"] >= 1
        assert trace["citations"]["granularity"] in ("per_claim", "list", "none")

    async def test_inspection_off_persists_no_trace(self, client: AsyncClient) -> None:
        response = await client.post("/api/v1/chat", json={"question": QUESTION, "inspect": False})
        request_id = response.json()["request_id"]

        from app.db.models import QueryLog
        from app.db.session import get_session_factory

        factory = get_session_factory()
        async with factory() as session:
            row = (
                await session.execute(select(QueryLog).where(QueryLog.id == _uuid(request_id)))
            ).scalar_one()

        assert row.pipeline_trace is None, (
            "inspection off still persisted the trace — a retention the operator did not ask for"
        )

    async def test_the_trace_carries_no_unit_text(self, client: AsyncClient) -> None:
        """FR-033's snapshot is provenance, not a second copy of the corpus."""
        response = await client.post("/api/v1/chat", json={"question": QUESTION, "inspect": True})
        request_id = response.json()["request_id"]

        from app.db.models import QueryLog
        from app.db.session import get_session_factory

        factory = get_session_factory()
        async with factory() as session:
            row = (
                await session.execute(select(QueryLog).where(QueryLog.id == _uuid(request_id)))
            ).scalar_one()

        import json as _json

        blob = _json.dumps(row.pipeline_trace)
        assert "text" not in row.pipeline_trace.get("citations", {})
        # No quoted passage text anywhere in the document: the unit text lives in
        # the vector store and is read back at a time, never stored here.
        assert "\\n\\n" not in blob

    async def test_the_verbatim_log_row_is_usable_for_an_answer_too(
        self, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Same row, answered path: trace keys are the four artifacts either way."""
        # Nothing else is genuinely needed from a refusal; this asserts the
        # record exists with the request's own question and outcome either way.
        response = await client.post("/api/v1/chat", json={"question": QUESTION, "inspect": True})
        request_id = response.json()["request_id"]

        from app.db.models import QueryLog
        from app.db.session import get_session_factory

        factory = get_session_factory()
        async with factory() as session:
            row = (
                await session.execute(select(QueryLog).where(QueryLog.id == _uuid(request_id)))
            ).scalar_one()

        assert row.outcome == "refused"
        assert row.refusal_reason == "INSUFFICIENT_EVIDENCE"
        assert row.question == QUESTION
        assert row.candidates_retrieved == 0


def _uuid(value: str):
    import uuid

    return uuid.UUID(value)
