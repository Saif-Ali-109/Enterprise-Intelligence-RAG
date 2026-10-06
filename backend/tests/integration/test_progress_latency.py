"""First-update latency (T125 / SC-016, contracts/events.md §1).

SC-016 asks for the **first** progress event within two seconds of request
receipt. Not the answer — the answer takes as long as retrieval and reranking
take, and the contract is explicit that there are no token deltas to render. What
must be fast is the *first sign of life*, because a user staring at an empty box
for four seconds concludes the system is broken.

**Measured two ways, because one of them cannot be measured in-process.**

`TestEmissionOrder` is deterministic and needs no clock: it records whether the
first event was emitted before the slow stage was entered. That is the property
SC-016 is really about, and it holds regardless of how fast the machine is.

`TestAgainstARunningServer` measures the number — against a real uvicorn, because
`httpx.ASGITransport` **buffers the whole response body** and replays it once the
app returns. An in-process timing test of a streaming endpoint therefore measures
the total latency and calls it the first-frame latency: measured here at 4.4s
against the same code that emits its first frame in 0.03s over a real socket. The
live check is skipped when no server is running, and it never fails the suite on
its absence — but it is the only assertion here that observes the number SC-016
states.

**The vendor is doubled in the deterministic test.** Generation is the slow
stage; a live-provider test of this property would mostly measure Groq's queue.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import AsyncIterator

import pytest
from app.api.events import EVENT_ORDER, EventEmitter

from tests.fixtures.database import describe_connection_failure, migrate_to_head, truncate_all

pytestmark = pytest.mark.performance

#: SC-016's budget for the first progress event.
FIRST_EVENT_BUDGET_SECONDS = 2.0

QUESTION = "How do I create a JQL filter in Jira?"

#: Where the live check looks for a server. Overridable so it can point at the
#: quickstart's own backend rather than assuming a port.
LIVE_BASE_URL = os.environ.get("E2E_BASE_URL", "http://127.0.0.1:8000")


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


class _SlowEmptyStore:
    """A reachable, empty index that takes its time to say so.

    The delay is on `search`, which is *after* `query_received` — so a pipeline
    that emits as it goes passes and one that awaited the pipeline would not.
    """

    def __init__(self) -> None:
        self.search_entered_at: float | None = None
        self.first_event_at: float | None = None

    async def search(self, **_kwargs: object) -> list[object]:
        self.search_entered_at = time.perf_counter()
        await asyncio.sleep(1.2)
        return []

    async def fetch(self, *_args: object, **_kwargs: object) -> list[object]:
        return []


class _RecordingProvider:
    """Classification with no network, so the test is about the pipeline's shape."""

    async def classify(self, *, system: str, user: str) -> dict[str, object]:
        return {
            "product": "jira",
            "category": "rest-api",
            "intent": "how_to",
            "entities": ["JQL"],
            "confidence": {"product": 0.95, "category": 0.9, "intent": 0.9, "entities": 0.8},
        }

    async def complete(self, **_kwargs: object):  # pragma: no cover - unreachable here
        raise AssertionError("an empty corpus must never reach generation")

    async def list_models(self) -> list[str]:
        return []


class TestEmissionOrder:
    async def test_the_first_event_precedes_the_slow_stage(self) -> None:
        """The property SC-016 rests on, with no clock involved.

        The store records when `search` was entered and the emitter's sink records
        when the first frame was queued. Asserting `first_event < search_entered`
        is stronger than a millisecond budget: it says the acceptance event is
        emitted *before the work begins*, so no amount of retrieval latency or
        provider queueing can delay it.
        """
        from app.chat.service import run_chat

        store = _SlowEmptyStore()
        timestamps: list[float] = []

        def sink(_event: object) -> None:
            timestamps.append(time.perf_counter())

        emitter = EventEmitter(request_id="0f3c1d2a-6b4e-4c1f-9a77-2b8e5d0c4a11", sink=sink)

        from app.db.session import get_session_factory

        factory = get_session_factory()
        async with factory() as session:
            await run_chat(
                QUESTION,
                session=session,
                store=store,  # type: ignore[arg-type]
                provider=_RecordingProvider(),  # type: ignore[arg-type]
                emit=emitter,
            )

        assert timestamps, "no event was emitted"
        assert store.search_entered_at is not None, "retrieval never ran"
        assert timestamps[0] < store.search_entered_at, (
            "the first event was emitted after retrieval started, so the wait is "
            "retrieval latency rather than a sign of life"
        )

    async def test_every_stage_is_reported_on_an_empty_corpus(self) -> None:
        """A refusal still reports all seven non-terminal events."""
        from app.chat.service import run_chat

        store = _SlowEmptyStore()
        emitter = EventEmitter(request_id="r")

        from app.db.session import get_session_factory

        factory = get_session_factory()
        async with factory() as session:
            result = await run_chat(
                QUESTION,
                session=session,
                store=store,  # type: ignore[arg-type]
                provider=_RecordingProvider(),  # type: ignore[arg-type]
                emit=emitter,
            )

        assert [event.name for event in emitter.events] == list(EVENT_ORDER)
        assert result.payload["outcome"] == "refused"


class TestAgainstARunningServer:
    """The number SC-016 states, measured over a real socket.

    Skipped rather than failed when no server is up: the deterministic test above
    covers the property, and a latency test that fails because a developer closed
    a terminal teaches nothing.
    """

    async def test_the_first_frame_arrives_within_two_seconds(self) -> None:
        from app.api import routes_chat_stream  # noqa: F401 - import check
        from app.db.session import get_session_factory

        del get_session_factory

        import httpx

        try:
            probe = await httpx.AsyncClient(timeout=5.0).get(f"{LIVE_BASE_URL}/api/v1/health")
        except Exception:  # noqa: BLE001 - no server is a skip, not a failure
            pytest.skip(f"no backend at {LIVE_BASE_URL}; run `make dev` or docker compose first")
        if probe.status_code >= 500:
            pytest.skip(f"the backend at {LIVE_BASE_URL} is not answering requests")

        started = time.perf_counter()
        first_at: float | None = None
        async with httpx.AsyncClient(timeout=120.0) as client:
            async with client.stream(
                "POST",
                f"{LIVE_BASE_URL}/api/v1/chat/stream",
                json={"question": QUESTION},
                headers={"Accept": "text/event-stream"},
            ) as response:
                async for line in response.aiter_lines():
                    if line.startswith("event: "):
                        first_at = time.perf_counter() - started
                        break

        assert first_at is not None, "the live stream produced no frames"
        assert first_at < FIRST_EVENT_BUDGET_SECONDS, (
            f"the first progress event took {first_at:.2f}s over a real socket; "
            f"SC-016 allows {FIRST_EVENT_BUDGET_SECONDS:.1f}s"
        )
