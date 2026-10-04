"""The vector service being unreachable (T099 / FR-061, FR-062, quickstart V10).

The requirement is narrow and it is absolute: when retrieval cannot happen, the
request fails with a request id, the failure is recorded, **no answer is
returned**, and nothing weaker is used in its place. There is no keyword-only
fallback here, no cached prior answer, no "best effort" corpus — because a
plausible answer from a degraded retrieval is the one failure this system exists
to make impossible, and it is invisible to the user unless the degradation is
never introduced at all.

**What is real:** the app, the error envelope, the error translation in the vector
adapter, and the registry. **What is doubled:** Pinecone, by raising the
transport failure the real client raises. Everything between that failure and the
client is the code under test.

**The doubled failure is the important half of this file.** A `search` that
returns an empty list is a *successful* empty search and must produce a refusal;
a `search` that raises must produce an error. Conflating them — by treating an
exception as "no results" — is the bug this file exists to prevent, so both are
asserted, and the mutation that turns one into the other fails loudly.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import httpx
import pytest
from httpx import AsyncClient
from sqlalchemy import select

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


class _UnavailableStore:
    """A store whose transport fails, the way an unreachable service does.

    `search` raises before anything is returned, so there is no partial result for
    a caller to be tempted by — which is what makes this double a faithful stand
    in rather than a convenient one.
    """

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc
        self.search_calls = 0
        self.fetch_calls = 0

    async def search(self, **_kwargs: object) -> list[object]:
        self.search_calls += 1
        raise self._exc

    async def fetch(self, *_args: object, **_kwargs: object) -> list[object]:
        self.fetch_calls += 1
        raise self._exc

    async def upsert(self, *_args: object, **_kwargs: object) -> object:
        raise self._exc

    async def delete(self, *_args: object, **_kwargs: object) -> int:
        raise self._exc

    async def delete_namespace(self, **_kwargs: object) -> None:
        raise self._exc

    async def describe(self) -> object:
        raise self._exc


@pytest.fixture
async def client(monkeypatch: pytest.MonkeyPatch, _schema: None) -> AsyncIterator[AsyncClient]:
    """The app with an unreachable vector service, plus the store itself.

    The patch point is the **route module's** name, not the vector module's: the
    route does `from app.retrieval.vector_store import get_vector_store` at import
    time, so it holds its own reference and a patch on the vector module is
    ignored. That was measured — the first run of this file passed against the
    real Pinecone and asserted a refusal that had nothing to do with the double.
    """
    from app.api import routes_chat
    from app.core.errors import VectorServiceUnavailable
    from app.main import create_app, lifespan

    store = _UnavailableStore(VectorServiceUnavailable("the vector service is unreachable"))
    monkeypatch.setattr(routes_chat, "get_vector_store", lambda: store)

    app = create_app(enable_probes=False)
    async with lifespan(app):
        async with AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as http:
            http.vector_store = store  # type: ignore[attr-defined]
            yield http


class TestVectorOutage:
    async def test_the_request_fails_with_a_request_id(self, client: AsyncClient) -> None:
        response = await client.post("/api/v1/chat", json={"question": "How do I rotate a key?"})

        assert response.status_code == 503, response.text
        error = response.json()["error"]
        assert error["code"] == "VECTOR_SERVICE_UNAVAILABLE"
        assert error["request_id"], "a failure nobody can trace is a support ticket"

    async def test_no_answer_is_returned(self, client: AsyncClient) -> None:
        """The point of the file. A 200 with an answer is the failure this forbids."""
        response = await client.post("/api/v1/chat", json={"question": "How do I rotate a key?"})

        body = response.text
        assert "answer" not in body.lower() or '"answer": null' in body
        assert "citation" not in body.lower() or '"citations": []' in body

    async def test_nothing_weaker_is_used_in_its_place(self, client: AsyncClient) -> None:
        """No fallback path runs: the only calls made are the failing ones.

        If a keyword fallback existed, this store would have seen a second kind
        of call — or a call with different arguments. Instead `search` is called,
        it raises, and the request ends there.
        """
        response = await client.post("/api/v1/chat", json={"question": "How do I rotate a key?"})

        store = client.vector_store
        assert store.search_calls >= 1
        assert store.fetch_calls == 0, "a degraded fetch ran after the search failed"
        assert response.status_code == 503

    async def test_the_failure_is_recorded_against_the_request(self, client: AsyncClient) -> None:
        """A recorded failure is what makes an outage diagnosable after the fact."""
        response = await client.post("/api/v1/chat", json={"question": "How do I rotate a key?"})
        request_id = response.json()["error"]["request_id"]

        from app.db.models import QueryLog
        from app.db.session import get_session_factory

        factory = get_session_factory()
        async with factory() as session:
            rows = (
                await session.execute(select(QueryLog).where(QueryLog.id == uuid.UUID(request_id)))
            ).scalars()
            recorded = list(rows)

        assert recorded, "the failure left no row to trace"
        assert recorded[0].outcome in {"failed", "error"}

    async def test_a_successful_empty_search_is_a_refusal_not_an_error(
        self, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The distinction this file's two halves exist to keep.

        A reachable index with nothing relevant is a *refusal* — a successful
        response with `outcome: "refused"`. An unreachable index is an *error*.
        Treating the exception as an empty result would turn every outage into a
        quiet "nothing found", which is how a system that is down reports itself
        as merely unhelpful.
        """
        from app.api import routes_chat
        from app.core.errors import VectorServiceUnavailable

        class _EmptyStore(_UnavailableStore):
            async def search(self, **_kwargs: object) -> list[object]:
                return []

        monkeypatch.setattr(
            routes_chat,
            "get_vector_store",
            lambda: _EmptyStore(VectorServiceUnavailable("unused")),
        )

        response = await client.post("/api/v1/chat", json={"question": "How do I rotate a key?"})

        assert response.status_code == 200, response.text
        assert response.json()["outcome"] == "refused"
