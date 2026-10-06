"""The empty corpus, and what a refusal says about it (T100 / edge case 1).

The distinction the contract requires is not two different refusals — it is two
different *reasons a client can read*. `candidates_retrieved: 0` means one of two
things: nothing has been indexed yet, or nothing relevant was found. An operator
who cannot tell them apart concludes the system is broken in one case and that
their question was wrong in the other, and both of those conclusions can be
false.

So this test asserts the two facts a client needs to tell them apart — the
refusal, and the corpus count beside it — rather than asserting a vocabulary the
contract does not have. `refusal_reason` has no "empty corpus" member, and
inventing one here would put a code in the response that the contract's enum does
not list.

Everything else is the ordinary claim: no answer, no citations, no substitute.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient

from tests.fixtures.database import describe_connection_failure, migrate_to_head, truncate_all

pytestmark = pytest.mark.integration


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


@pytest.fixture
async def client(monkeypatch: pytest.MonkeyPatch, _schema: None) -> AsyncIterator[AsyncClient]:
    """The real app over an empty registry, with the vendors doubled.

    The store is doubled because an empty corpus is the *registry's* state, not
    the index's: seeding vectors without registry rows would make the retriever
    drop every candidate for a reason unrelated to what this test is about, and
    a corpus whose vectors exist while its rows do not is precisely the
    situation T099's sibling — the orphaned vector — refuses to answer from.
    """
    from app.main import create_app, lifespan
    from app.retrieval import vector_store

    from tests.fixtures.vector_store import RecordingVectorStore

    empty = RecordingVectorStore()
    monkeypatch.setattr(vector_store, "get_vector_store", lambda: empty)

    app = create_app(enable_probes=False)
    async with lifespan(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as http:
            yield http


class TestEmptyCorpus:
    async def test_a_question_against_an_empty_corpus_is_refused(self, client: AsyncClient) -> None:
        response = await client.post(
            "/api/v1/chat", json={"question": "How do I rotate an API token?"}
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["outcome"] == "refused"
        assert body["answer"] is None
        assert body["citations"] == []
        assert body["refusal_reason"] == "INSUFFICIENT_EVIDENCE"

    async def test_the_refusal_reports_an_empty_pool(self, client: AsyncClient) -> None:
        """`candidates_retrieved: 0` is the half a client reads first."""
        response = await client.post("/api/v1/chat", json={"question": "What is a Jira board?"})
        searched = response.json()["searched"]

        assert searched["candidates_retrieved"] == 0
        assert searched["candidates_reranked"] == 0
        assert searched["evidence_selected"] == 0
        assert searched["queries"], "a refusal that names no query is a dead end (FR-008)"

    async def test_the_corpus_count_tells_nothing_indexed_from_nothing_relevant(
        self, client: AsyncClient
    ) -> None:
        """The other half: with no units registered, the two are distinguishable.

        This is the assertion that makes "nothing indexed yet" sayable. A client
        reads `unit_count` from `/config` beside the refusal's
        `candidates_retrieved` and can tell an unbuilt corpus from a question
        the corpus does not cover — without a new reason code, and without the
        backend inventing one.
        """
        chat = await client.post("/api/v1/chat", json={"question": "What is a Jira board?"})
        config = await client.get("/api/v1/config")

        assert chat.status_code == 200
        assert config.status_code == 200
        corpus = config.json()["corpus"]
        assert corpus["unit_count"] == 0
        assert corpus["document_count"] == 0
        assert chat.json()["searched"]["candidates_retrieved"] == 0

    async def test_no_lead_is_offered_when_nothing_was_retrieved(self, client: AsyncClient) -> None:
        """FR-008: leads are weakly *related* sources. Nothing was, so there are none.

        Offering a source here would be inventing a pointer to make the refusal
        feel productive, which is the same fabrication as an invented citation
        with a different shape.
        """
        response = await client.post("/api/v1/chat", json={"question": "What is a Jira board?"})

        assert response.json()["leads"] == []

    async def test_the_response_validates_against_the_contract_schema(
        self, client: AsyncClient
    ) -> None:
        """The refusal is read as raw JSON and as the model, as elsewhere."""
        from app.schemas.chat import AskResponse

        response = await client.post("/api/v1/chat", json={"question": "What is a Jira board?"})

        model = AskResponse.model_validate(response.json())
        assert model.outcome == "refused"
        assert model.answer is None


class TestQuestionShape:
    async def test_an_empty_question_is_refused_by_validation(self, client: AsyncClient) -> None:
        """Bounded at the boundary (FR-043), and never guessed into a question.

        FastAPI's 422 is translated to the API's own `VALIDATION_ERROR` at 400 —
        the app answers one error envelope for every refusal, and a client that
        had to know which layer produced the code would be coupled to both.
        """
        response = await client.post("/api/v1/chat", json={"question": ""})

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "VALIDATION_ERROR"

    async def test_an_overlong_question_is_refused_by_validation(self, client: AsyncClient) -> None:
        response = await client.post("/api/v1/chat", json={"question": "x" * 2001})

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "VALIDATION_ERROR"

    async def test_a_request_id_is_returned_even_for_a_refusal(self, client: AsyncClient) -> None:
        """A refusal someone can trace to a log line is worth the two fields."""
        response = await client.post("/api/v1/chat", json={"question": "What is a Jira board?"})

        request_id = response.json()["request_id"]
        assert uuid.UUID(request_id)  # raises if it is not one
