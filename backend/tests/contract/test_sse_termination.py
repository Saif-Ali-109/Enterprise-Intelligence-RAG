"""SSE termination (T123 / contracts/events.md §5).

The rule is narrow and a client depends on it completely: **exactly one terminal
event — `answer_completed` or `error` — never both, never neither, and `[DONE]`
after it.**

"Never neither" is the failure that matters and the one a happy-path test cannot
see. A stream that closes without a terminal frame leaves a client that followed
§6 waiting forever, and the UI's last honest state is a spinner — so this file
asserts on the *absence* of things as much as their presence.

This is an integration test because termination is a property of the transport as
much as of the emitter: the frames have to survive FastAPI's `StreamingResponse`,
httpx's chunking, and the task that runs the pipeline beside the body. A test that
called the emitter directly would pass while the endpoint hung.

The corpus is empty and the vendors are doubled, so the path exercised here is the
refusal — which is also the path most likely to skip an event, since four stages
produced nothing.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from tests.fixtures.database import describe_connection_failure, migrate_to_head, truncate_all

pytestmark = pytest.mark.contract

QUESTION = "How do I create a JQL filter in Jira?"


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


def _parse_frames(body: str) -> list[tuple[str, str]]:
    """`[(event, data)]` from a raw SSE body.

    Hand-parsed rather than with an SSE library, deliberately: the contract's
    client requirements are about buffering until a blank line, and a test that
    used a library would be testing the library's tolerance rather than whether
    the server emits well-formed frames.
    """
    frames: list[tuple[str, str]] = []
    for block in body.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        name = ""
        data_lines: list[str] = []
        for line in block.split("\n"):
            if line.startswith("event: "):
                name = line.removeprefix("event: ")
            elif line.startswith("data: "):
                data_lines.append(line.removeprefix("data: "))
        frames.append((name, "\n".join(data_lines)))
    return frames


class _RefusingStore:
    """A reachable store with nothing in it: the refusal path, honestly reached."""

    async def search(self, **_kwargs: object) -> list[object]:
        return []

    async def fetch(self, *_args: object, **_kwargs: object) -> list[object]:
        return []


@pytest.fixture
async def client(monkeypatch: pytest.MonkeyPatch, _schema: None) -> AsyncIterator[AsyncClient]:
    """The real app with a reachable-but-empty index.

    The patch point is the route module's own reference, for the reason
    `test_vector_outage.py` documents: the routes do
    `from app.retrieval.vector_store import get_vector_store` at import time.
    """
    from app.api import routes_chat, routes_chat_stream
    from app.main import create_app, lifespan

    store = _RefusingStore()
    monkeypatch.setattr(routes_chat, "get_vector_store", lambda: store)
    monkeypatch.setattr(routes_chat_stream, "get_vector_store", lambda: store)

    app = create_app(enable_probes=False)
    async with lifespan(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as http:
            yield http


class TestTermination:
    async def test_the_stream_ends_with_one_terminal_event_and_done(
        self, client: AsyncClient
    ) -> None:
        response = await client.post(
            "/api/v1/chat/stream",
            json={"question": QUESTION},
            headers={"Accept": "text/event-stream"},
        )
        assert response.status_code == 200, response.text
        frames = _parse_frames(response.text)
        names = [name for name, _data in frames if name]

        terminal = [name for name in names if name in ("answer_completed", "error")]
        assert terminal == ["answer_completed"], f"expected one terminal event, saw {terminal}"
        assert frames[-1][0] == "", (
            f"the last frame should be the terminator, saw {frames[-1][0]!r}"
        )
        assert frames[-1][1] == "[DONE]"

    async def test_every_payload_carries_the_request_id(self, client: AsyncClient) -> None:
        """events.md §2: the correlation key is on every frame, and it is the audit id."""
        response = await client.post(
            "/api/v1/chat/stream",
            json={"question": QUESTION},
            headers={"Accept": "text/event-stream"},
        )
        frames = [f for f in _parse_frames(response.text) if f[0]]
        request_ids = {json.loads(data)["request_id"] for _name, data in frames}

        assert len(request_ids) == 1, f"the stream carried {len(request_ids)} request ids"

        from app.db.models import QueryLog
        from app.db.session import get_session_factory

        factory = get_session_factory()
        async with factory() as session:
            rows = (
                await session.execute(
                    select(QueryLog.id).where(QueryLog.id == uuid_of(request_ids.pop()))
                )
            ).all()
        assert rows, "the stream's request id has no audit row (FR-047, FR-048)"

    async def test_the_response_declares_the_contract_headers(self, client: AsyncClient) -> None:
        """R-011. Without these an intermediary buffers the stream into one clump."""
        response = await client.post(
            "/api/v1/chat/stream",
            json={"question": QUESTION},
            headers={"Accept": "text/event-stream"},
        )
        headers = response.headers

        assert headers["content-type"].startswith("text/event-stream")
        assert "charset=utf-8" in headers["content-type"]
        assert headers["cache-control"] == "no-cache, no-transform"
        assert headers["x-accel-buffering"] == "no"

    async def test_a_failed_request_terminates_rather_than_hanging(
        self, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A pipeline that raises outside the error vocabulary still terminates.

        The synthesised `error` frame is what a client needs: without it the stream
        would end silently, which §5 calls a client-visible failure. The patch is
        on the *route* module — it holds its own reference to `run_chat`, so
        patching `app.chat.service` would leave the real pipeline running and this
        test would assert nothing about the failure path.
        """
        from app.api import routes_chat_stream

        async def explode(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("the pipeline fell over outside the error vocabulary")

        monkeypatch.setattr(routes_chat_stream, "run_chat", explode)

        response = await client.post(
            "/api/v1/chat/stream",
            json={"question": QUESTION},
            headers={"Accept": "text/event-stream"},
        )
        frames = _parse_frames(response.text)
        names = [name for name, _data in frames if name]

        assert names == ["error"], f"a dead pipeline produced {names}"
        assert frames[-1][1] == "[DONE]"
        payload = json.loads(frames[-2][1])
        assert payload["code"] == "INTERNAL_ERROR"
        assert "answer" not in payload, "an error frame carries no partial answer (FR-061)"


def uuid_of(value: str):
    """`uuid.UUID` from a request id, raising if it is not one."""
    import uuid

    return uuid.UUID(value)
