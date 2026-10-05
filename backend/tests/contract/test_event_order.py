"""The event stream's normative order (T120 / FR-035, contracts/events.md §3).

The order is the one property a client depends on that no payload shape can
express, so it is asserted as a *sequence*: the exact names, in the exact order,
for both outcomes, with the terminal event last.

**Both outcomes, because the contract says both.** An order that holds for an
answer and not for a refusal is the common shape of this bug: the answer path
emits eight events and the refusal path emits three and closes, which every
happy-path test passes.

**The order is enforced in code, not only asserted here.** `EventEmitter.emit`
refuses a name that is not the next one, so this file is checking that the service
emits in order — not that a list constant happens to be in order. Both halves
matter: the constant documents the contract, and the emitter is what makes a
violation impossible.

**Run without a database, a network, or a provider.** The emitter is the unit
under test; the pipeline's contributions to it are covered by the integration
suite, where an SSE request is read end to end.
"""

from __future__ import annotations

import pytest
from app.api.events import EVENT_ORDER, TERMINAL, EventEmitter, EventName, placeholder_payload

pytestmark = pytest.mark.contract

EXPECTED = (
    "query_received",
    "query_analyzed",
    "retrieval_started",
    "retrieval_completed",
    "reranking_completed",
    "generation_started",
    "citation_validation",
    "answer_completed",
)


class TestTheContractConstant:
    def test_the_declared_order_is_the_normative_one(self) -> None:
        assert tuple(name.value for name in EVENT_ORDER) == EXPECTED

    def test_only_two_events_are_terminal(self) -> None:
        assert {name.value for name in TERMINAL} == {"answer_completed", "error"}


class TestOrderOnEveryPath:
    def _run_all(self, *, terminal: EventName) -> list[str]:
        emitter = EventEmitter(request_id="0f3c1d2a-6b4e-4c1f-9a77-2b8e5d0c4a11")
        for name in EVENT_ORDER:
            fields = placeholder_payload(name) if name is terminal else {}
            emitter.emit(name, **fields)
        return [event.name.value for event in emitter.events]

    def test_the_answered_path_emits_all_eight_in_order(self) -> None:
        assert self._run_all(terminal=EventName.ANSWER_COMPLETED) == list(EXPECTED)

    def test_the_error_path_still_emits_all_eight_in_order(self) -> None:
        """`error` replaces event 8 — it does not shorten the stream to three frames.

        A client that switches on the event name to learn *which* stage ran must
        see the same seven on both paths, or it will report a fault as a failure
        to reach retrieval.
        """
        assert self._run_all(terminal=EventName.ERROR) == list(EXPECTED)

    def test_a_skipped_stage_is_emitted_with_zero_counts(self) -> None:
        """events.md §3: no event is skipped within 1–7.

        The refusal path — nothing retrieved, so nothing reranked, so nothing
        generated — still produces every frame, which is how a client learns the
        difference between a fault and a stage that ran and found nothing.
        """
        emitter = EventEmitter(request_id="r")
        emitter.emit(EventName.QUERY_RECEIVED, question="q", inspect=False)
        emitter.emit_skipped_through(EventName.ANSWER_COMPLETED, outcome="refused")

        assert [event.name.value for event in emitter.events] == list(EXPECTED)
        assert emitter.events[3].data["candidates_retrieved"] == 0
        assert emitter.events[4].data["candidates_reranked"] == 0
        assert emitter.events[5].data["evidence_count"] == 0
        assert emitter.events[6].data["citations_valid"] == 0

    def test_every_skipped_frame_carries_its_own_zero_fields(self) -> None:
        """The final frame a client indexes into must not arrive nearly empty.

        The skipped-through target is emitted with the caller's fields merged over
        its placeholder; emitting `**fields` alone once produced a
        `citation_validation` frame carrying nothing but a `request_id`.
        """
        emitter = EventEmitter(request_id="r")
        emitter.emit(EventName.QUERY_RECEIVED, question="q", inspect=False)
        emitter.emit_skipped_through(EventName.ANSWER_COMPLETED, outcome="refused", answer=None)

        for event in emitter.events[1:]:
            assert len(event.data) > 1, f"{event.name.value} arrived with only a request_id"

    def test_a_stage_that_never_ran_reports_no_attempt_rather_than_attempt_zero(self) -> None:
        """events.md §4 gives `attempt` the domain 1 or 2, and §4's own convention
        is that an absent field means *not determined* while `0` means *determined
        to be zero*. Generation never started, so no attempt was made."""
        emitter = EventEmitter(request_id="r")
        emitter.emit(EventName.QUERY_RECEIVED, question="q", inspect=False)
        emitter.emit_skipped_through(EventName.ANSWER_COMPLETED, outcome="refused")

        generation = next(e for e in emitter.events if e.name is EventName.GENERATION_STARTED)
        assert generation.data["evidence_count"] == 0
        assert "attempt" not in generation.data


class TestTheEmitterRefusesViolations:
    def test_an_out_of_order_event_is_refused(self) -> None:
        emitter = EventEmitter(request_id="r")
        with pytest.raises(ValueError, match="event order violated"):
            emitter.emit(EventName.RETRIEVAL_STARTED)

    def test_an_event_after_the_terminal_one_is_refused(self) -> None:
        emitter = EventEmitter(request_id="r")
        for name in EVENT_ORDER:
            emitter.emit(name)
        with pytest.raises(ValueError, match="already terminal"):
            emitter.emit(EventName.QUERY_RECEIVED)

    def test_a_second_terminal_event_is_refused(self) -> None:
        emitter = EventEmitter(request_id="r")
        for name in EVENT_ORDER:
            emitter.emit(name)
        with pytest.raises(ValueError, match="already terminal"):
            emitter.emit(EventName.ERROR)

    def test_the_request_id_is_stamped_on_every_payload(self) -> None:
        emitter = EventEmitter(request_id="abc")
        for name in EVENT_ORDER:
            emitter.emit(name)
        assert all(event.data["request_id"] == "abc" for event in emitter.events)

    def test_a_sequence_number_counts_from_one(self) -> None:
        emitter = EventEmitter(request_id="r")
        for name in EVENT_ORDER:
            emitter.emit(name)
        assert [event.sequence for event in emitter.events] == [1, 2, 3, 4, 5, 6, 7, 8]


class TestFrameFormat:
    def test_a_frame_is_one_event_one_line_of_json_and_a_blank_line(self) -> None:
        emitter = EventEmitter(request_id="r")
        event = emitter.emit(EventName.QUERY_RECEIVED, question="q", inspect=False)
        frame = event.frame()

        assert frame.startswith("event: query_received\n")
        assert frame.endswith("\n\n")
        data_line = frame.split("data: ", 1)[1].strip()
        assert "\n" not in data_line, "a pretty-printed payload corrupts the frame"
        assert '"question":"q"' in data_line

    def test_a_newline_inside_a_value_does_not_break_the_frame(self) -> None:
        """A question with a newline in it is a frame with a JSON-escaped newline.

        `json.dumps` escapes it; a naive concatenation would split the frame and a
        client would parse half a payload as the whole one.
        """
        emitter = EventEmitter(request_id="r")
        event = emitter.emit(
            EventName.QUERY_RECEIVED, question="first line\nsecond line", inspect=False
        )
        frame = event.frame()

        assert frame.count("\n\n") == 1
        assert len(frame.strip().split("\n\n")[0].split("\n")) == 2


class TestNoReasoningIsEmitted:
    def test_a_payload_with_a_reasoning_key_is_refused(self) -> None:
        """FR-034 is absolute, and the check is at the emitter rather than the service.

        A stage that grew a `reasoning` field would satisfy every other test in
        this file, so the boundary refuses it where it would be caught by a
        contract test rather than by a user. Emitted in order, so the *reasoning*
        check is what fails and not the ordering one.
        """
        emitter = EventEmitter(request_id="r")
        emitter.emit(EventName.QUERY_RECEIVED, question="q", inspect=False)
        with pytest.raises(ValueError, match="reasoning"):
            emitter.emit(EventName.QUERY_ANALYZED, reasoning="because I said so")

    def test_an_allowed_payload_is_not_refused(self) -> None:
        emitter = EventEmitter(request_id="r")
        emitter.emit(EventName.QUERY_RECEIVED, question="q", inspect=False)
        emitter.emit(
            EventName.QUERY_ANALYZED,
            detected_product="jira",
            ambiguity_note=None,
            filters_suppressed=False,
        )
