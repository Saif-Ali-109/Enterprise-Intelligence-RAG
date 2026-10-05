"""The progress event stream (T126 / FR-035, contracts/events.md).

Eight events in a fixed order, plus a ninth terminal `error`. This module owns
three things and nothing else: the order, the frames, and the refusal to emit
anything else.

**The order is enforced, not documented.** `EventEmitter.emit` refuses a name that
is not the next one in `EVENT_ORDER`, and refuses anything after a terminal event.
A stream that emitted `answer_completed` before `retrieval_started` would satisfy
every individual payload shape and break every client, and the shape checks would
all pass. Making the emitter the only way to produce an event means the guarantee
lives in code that a reviewer can read in one screen.

**A stage that produced nothing still emits.** `retrieval_completed` with
`candidates_retrieved: 0` is how a client tells "nothing indexed yet" from
"nothing relevant found" — two states the spec requires be distinguishable. So the
service emits all seven non-terminal events on every path, including the ones that
produced nothing, and the emitter's order check is what guarantees the count.

**No event may carry a reasoning channel.** The payloads are checked against the
same key-predicate the provider's boundary uses, so FR-034's guarantee does not
depend on the service remembering not to add one: an event carrying
`reasoning`/`thought`/`chain_of_thought` raises rather than reaching a client
(contracts/events.md §7).

**One frame format, written in one place.** `event:`, one line of compact JSON,
blank line. A pretty-printed payload would be split across `data:` lines and
corrupt the frame, and that bug is invisible in a payload test.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from app.generation.provider import find_reasoning_keys


class EventName(StrEnum):
    """The nine event names. Eight in order, plus the terminal `error`."""

    QUERY_RECEIVED = "query_received"
    QUERY_ANALYZED = "query_analyzed"
    RETRIEVAL_STARTED = "retrieval_started"
    RETRIEVAL_COMPLETED = "retrieval_completed"
    RERANKING_COMPLETED = "reranking_completed"
    GENERATION_STARTED = "generation_started"
    CITATION_VALIDATION = "citation_validation"
    ANSWER_COMPLETED = "answer_completed"
    ERROR = "error"


#: The normative sequence. `error` is terminal and *replaces* event 8, so it is not
#: part of the ordered prefix a client waits through — see `is_terminal`.
EVENT_ORDER = (
    EventName.QUERY_RECEIVED,
    EventName.QUERY_ANALYZED,
    EventName.RETRIEVAL_STARTED,
    EventName.RETRIEVAL_COMPLETED,
    EventName.RERANKING_COMPLETED,
    EventName.GENERATION_STARTED,
    EventName.CITATION_VALIDATION,
    EventName.ANSWER_COMPLETED,
)

TERMINAL: frozenset[EventName] = frozenset({EventName.ANSWER_COMPLETED, EventName.ERROR})

#: The payload every event carries, per events.md §2.
REQUEST_ID = "request_id"

#: Sent after the terminal event, then the stream closes (events.md §5).
DONE = "[DONE]"


@dataclass(frozen=True, slots=True)
class StreamEvent:
    """One frame's worth of state: the name, the payload, and where it sits."""

    name: EventName
    data: dict[str, Any]
    sequence: int

    def frame(self) -> str:
        """The SSE frame. Compact JSON, one line, blank-line terminated."""
        return f"event: {self.name.value}\ndata: {json.dumps(self.data, separators=(',', ':'))}\n\n"

    @property
    def is_terminal(self) -> bool:
        return self.name in TERMINAL


@dataclass
class EventEmitter:
    """Emits the stream's events in order, and refuses anything else.

    `emit` is synchronous and does no I/O: the transport decides how frames reach
    the client (an `asyncio.Queue` for SSE, nothing at all for the non-streaming
    route), and the emitter only guarantees order and shape. That separation is
    what lets `run_chat` be the *same* pipeline for both (R-007) — the streaming
    route passes an emitter and the JSON route passes `None`.

    `emit_skipped_through(name)` is how a stage that never ran still gets its
    event: the service declares "everything up to and including this event
    happened, with zero counts", which is what keeps events 1–7 unskippable
    rather than merely usually-present.

    `sink` is where frames go as they are produced — an `asyncio.Queue.put_nowait`
    for SSE, or nothing. It lives here rather than in a subclass because the
    service receives an `EventEmitter` and nothing else: a wrapper would have to
    forward `emit`, `emit_skipped_through` and `events`, and the one method it
    forgot to forward would be a stage that silently stops reaching the client.
    """

    request_id: str
    sink: Callable[[StreamEvent], None] | None = None
    events: list[StreamEvent] = field(default_factory=list)

    @property
    def next_expected(self) -> EventName | None:
        if len(self.events) < len(EVENT_ORDER):
            return EVENT_ORDER[len(self.events)]
        return None

    def emit(self, name: EventName, /, **fields: Any) -> StreamEvent:
        """One event, in order, with the request id stamped on it."""
        expected = self.next_expected
        if expected is None:
            raise ValueError(
                f"event {self.events[-1].name.value!r} was already terminal; "
                f"{name.value!r} cannot follow it"
            )
        if name is EventName.ERROR:
            # `error` replaces event 8 rather than following it, so it may only
            # arrive while event 8 is still outstanding.
            if self.events and self.events[-1].is_terminal:
                raise ValueError("error after a terminal event")
        elif name is not expected:
            raise ValueError(
                f"event order violated: expected {expected.value!r}, got {name.value!r}"
            )

        data = {REQUEST_ID: self.request_id, **fields}
        leaks = find_reasoning_keys(data)
        if leaks:
            raise ValueError(f"an event payload exposed a reasoning channel: {leaks}")

        event = StreamEvent(name=name, data=data, sequence=len(self.events) + 1)
        self.events.append(event)
        if self.sink is not None:
            self.sink(event)
        return event

    def emit_skipped_through(self, name: EventName, /, **fields: Any) -> None:
        """Emit every outstanding event up to `name`, with the given payload.

        Used on the paths where a stage did not run — the retrieval found
        nothing, so there is nothing to rerank, so there is nothing to generate.
        The contract requires those events to appear with zero counts rather than
        be skipped (events.md §3), and this is how they appear without the service
        having to spell out seven emissions per early return.
        """
        while True:
            expected = self.next_expected
            if expected is None:
                return
            if expected is name:
                # Merged, not replaced: the caller's values win where it has any,
                # and the placeholder fills in the rest. Passing `**fields` alone
                # emitted the final frame with nothing but a `request_id`, which is
                # the one frame a client is guaranteed to index into.
                self.emit(name, **{**placeholder_payload(name), **fields})
                return
            # Zero counts and empty structures: the honest payload for a stage
            # that did not run. Each placeholder event carries the same zero-count
            # fields the contract names for it, so a client reading the stream
            # sees "nothing retrieved" rather than a missing field.
            self.emit(expected, **placeholder_payload(expected))


def placeholder_payload(name: EventName) -> dict[str, Any]:
    """The zero-count payload for an event whose stage never ran.

    One function rather than seven literals, because these are read by clients
    that index into them: a missing `candidates_retrieved` is a client crash,
    while `0` is the whole point of the event.
    """
    if name is EventName.QUERY_ANALYZED:
        return {
            "detected_product": None,
            "detected_category": None,
            "intent": None,
            "entities": [],
            "classification_confidence": {},
            "rewrite_queries": [],
            "applied_filters": {},
            "filters_suppressed": True,
            "ambiguity_note": None,
        }
    if name is EventName.RETRIEVAL_STARTED:
        return {"candidate_pool": 0, "query_count": 0}
    if name is EventName.RETRIEVAL_COMPLETED:
        return {"candidates_retrieved": 0, "queries_executed": []}
    if name is EventName.RERANKING_COMPLETED:
        return {"candidates_reranked": 0, "rerank_model": None}
    if name is EventName.GENERATION_STARTED:
        # `attempt` is deliberately absent, not zero. events.md §4 declares the
        # field's domain as 1 or 2, and events.md's own convention is that a
        # missing field means *not determined* while `0` means *determined to be
        # zero*. Generation never started here, so no attempt was made: that is
        # not determined, and emitting `0` would put a value outside the
        # contract's domain into every refusal's stream.
        return {"evidence_count": 0}
    if name is EventName.CITATION_VALIDATION:
        return {
            "citations_valid": 0,
            "citations_stripped": 0,
            "citations_repaired": 0,
            "rejected_identifiers": [],
        }
    return {}


async def sse_frames(events: AsyncIterator[StreamEvent]) -> AsyncIterator[str]:
    """Yield rendered frames. Kept beside the emitter so the format has one home."""
    async for event in events:
        yield event.frame()


__all__ = [
    "DONE",
    "EVENT_ORDER",
    "TERMINAL",
    "EventEmitter",
    "EventName",
    "StreamEvent",
    "placeholder_payload",
    "sse_frames",
]
