"""No reasoning, anywhere, ever (T121 / FR-034, quickstart V7).

FR-034 is absolute: no event payload, response field, or logged record anywhere
contains a reasoning, thought, chain-of-thought, or deliberation field —
including under inspection mode and debug logging.

**The test asserts absence, which is what the requirement is.** That is weaker
than it sounds to read back: a test for the *presence* of an answer tells you the
answer arrived; a test for the absence of a field tells you that, in every
payload, a deep key walk over the JSON found nothing. The two modes of this test
exist because the failure mode it guards against does not announce itself in one
place — it leaks at whichever seam was quiet that day.

**Walked, not searched for.** A grep for `"reasoning"` misses a nested
`metadata.trace.reasoning`; a recursive key walk over the parsed object cannot.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient

from tests.fixtures.credentials import seed_placeholder_credentials
from tests.fixtures.database import describe_connection_failure, migrate_to_head, truncate_all

seed_placeholder_credentials()

pytestmark = pytest.mark.contract

#: The vocabulary the contract closes the door to (events.md §7, R-014).
_FORBIDDEN = re.compile(
    r"^(reasoning|reasoning_content|reasoning_details|thought|thoughts|chain_of_thought|deliberation|reflection|scratchpad)$",
    re.IGNORECASE,
)


def _find_forbidden_keys(value: object, path: str = "$") -> list[str]:
    """Every place a reasoning field appears in the decoded payload."""
    found: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            if isinstance(key, str) and _FORBIDDEN.match(key):
                found.append(f"{path}.{key}")
            found.extend(_find_forbidden_keys(child, f"{path}.{key}"))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found.extend(_find_forbidden_keys(child, f"{path}[{index}]"))
    return found


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


class TestNoReasoningLeaks:
    async def test_every_event_payload_is_free_of_reasoning(self, client: AsyncClient) -> None:
        response = await client.post(
            "/api/v1/chat/stream",
            json={"question": "How do I create a JQL filter?", "inspect": True},
            headers={"Accept": "text/event-stream"},
        )
        assert response.status_code == 200

        payloads = []
        for block in response.text.split("\n\n"):
            for line in block.split("\n"):
                if line.startswith("data: ") and not line.startswith("data: [DONE]"):
                    payloads.append(json.loads(line.removeprefix("data: ")))

        assert payloads, "the inspection-mode stream produced no events to inspect"
        for payload in payloads:
            leaks = _find_forbidden_keys(payload)
            assert leaks == [], f"reasoning field in event payload: {leaks}"

    async def test_the_json_response_is_free_of_reasoning_under_inspection(
        self, client: AsyncClient
    ) -> None:
        response = await client.post(
            "/api/v1/chat",
            json={"question": "How do I create a JQL filter?", "inspect": True},
        )
        assert response.status_code == 200

        body = response.json()
        assert body["trace"] is not None, "inspection mode must produce a trace to inspect"
        leaks = _find_forbidden_keys(body)
        assert leaks == [], f"reasoning field in the response body: {leaks}"

    async def test_log_records_are_free_of_reasoning(
        self, client: AsyncClient, caplog: pytest.LogCaptureFixture
    ) -> None:
        """FR-034 names log records as much as it names payloads.

        Capture at DEBUG, because the temptation to log a stage's raw model
        output is strongest when a developer is chasing a retrieval bug — which
        is also when the log is most likely to be shipped somewhere real.
        """
        with caplog.at_level(logging.DEBUG):
            await client.post(
                "/api/v1/chat",
                json={"question": "How do I create a JQL filter?", "inspect": True},
            )

        for record in caplog.records:
            message = record.getMessage()
            for token in _FORBIDDEN.finditer(message):
                # Mentions in prose ("provider reasoning discarded at the adapter
                # boundary") are the *proof* the control ran; a forbidden field is
                # where the prose would be a key.
                key_shaped = re.search(rf"['\"]{re.escape(token.group(0))}['\"]\s*:", message)
                assert key_shaped is None, (
                    f"a log record carried a reasoning field: {message[:120]}"
                )

    async def test_debug_logging_does_not_expose_reasoning(
        self, client: AsyncClient, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The FR-034 guarantee holds when logging is set to its most verbose."""
        with caplog.at_level(logging.DEBUG, logger="app"):
            await client.post(
                "/api/v1/chat/stream",
                json={"question": "How do I create a JQL filter?", "inspect": True},
                headers={"Accept": "text/event-stream"},
            )

        for record in caplog.records:
            try:
                vars(record)
            except Exception:  # noqa: BLE001
                continue
            extra = {k: v for k, v in vars(record).items() if k not in logging.LogRecord.__dict__}
            for key in extra:
                assert not _FORBIDDEN.match(key), f"a log record's extra carried {key}"
