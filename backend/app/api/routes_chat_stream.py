"""`POST /chat/stream` — the same pipeline, delivered as events (T127).

**The stream is transport, not a second pipeline.** `run_chat` is called once and
is the same function `POST /chat` calls; the only difference is that this route
hands it an `EventEmitter` with a queue as its sink while the JSON route hands it
`None` (R-007). The evaluation runner therefore measures the work a user sees, and
there is no second implementation that can drift from the first.

**Events go out as they happen, not at the end.** The emitter's sink writes into
an `asyncio.Queue` from inside the pipeline, and the response body drains that
queue — so `query_received` reaches the client while retrieval is still running,
which is the whole point of a progress stream (SC-016's two-second budget is about
this, not about the total). A generator that awaited the whole pipeline and then
yielded eight frames would satisfy every payload shape and fail the requirement.

**The pipeline runs as a task beside the body.** `StreamingResponse` consumes the
generator, and the generator would otherwise block on the queue while the only
coroutine able to fill it is the one awaiting it. Two coroutines, one queue.

**Three headers, all load-bearing (R-011).** `Cache-Control: no-cache,
no-transform` and `X-Accel-Buffering: no` are what stop an intermediate proxy
buffering the response into one clump at the end, which looks exactly like a
broken backend.

**Exactly one terminal event, then `[DONE]`.** The terminal frame is emitted by
the code that produced the answer or the error, and `[DONE]` is appended by the
transport afterwards — so a client treats either as the end of the stream without
guessing (events.md §5). If the pipeline somehow ends with no terminal event, one
is synthesised rather than leaving the client waiting for a frame that will never
come.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from app.api.deps import Session
from app.api.events import DONE, EventEmitter, EventName
from app.chat.service import run_chat
from app.core.logging import get_logger, get_request_id, new_request_id
from app.core.ratelimit import enforce_ask
from app.generation.provider import get_language_model
from app.retrieval.vector_store import get_vector_store
from app.schemas.chat import AskRequest

_log = get_logger("api.chat")

router = APIRouter(tags=["chat"])

#: R-011: the headers, verbatim from events.md §1. `charset` included because the
#: contract declares it and a client that guesses wrong renders Atlassian's
#: em-dashes as mojibake.
SSE_MEDIA_TYPE = "text/event-stream; charset=utf-8"
SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "X-Accel-Buffering": "no",
    "Connection": "keep-alive",
}


@router.post(
    "/chat/stream",
    summary="Ask a question and receive pipeline progress as Server-Sent Events",
    description=(
        "Streams the eight normative events of `contracts/events.md` §3 in order, terminated by "
        "`answer_completed` or `error` and then `data: [DONE]`. No event is skipped within 1–7: a "
        "stage that produced nothing reports a zero count. No event carries model reasoning "
        "(FR-034). Executes the identical pipeline `POST /chat` does."
    ),
    dependencies=[Depends(enforce_ask)],
)
async def ask_stream(request: AskRequest, session: Session) -> StreamingResponse:
    """The stream's transport. The pipeline lives in `run_chat`, unchanged."""
    # One request id for the stream, the audit row and the log lines (FR-047).
    request_id = get_request_id() or new_request_id()
    queue: asyncio.Queue[str] = asyncio.Queue()

    def sink(event: object) -> None:
        queue.put_nowait(event.frame())  # type: ignore[attr-defined]

    emitter = EventEmitter(request_id=request_id, sink=sink)

    async def pipeline() -> None:
        try:
            await run_chat(
                request.question,
                session=session,
                store=get_vector_store(),
                provider=get_language_model(),
                inspect=request.inspect,
                emit=emitter,
            )
        except Exception as exc:  # noqa: BLE001 - the stream must still terminate
            _log.error(
                "the streaming pipeline raised outside the error vocabulary",
                extra={"request_id": request_id, "error": str(exc)[:200]},
            )
            emitter.emit(
                EventName.ERROR,
                code="INTERNAL_ERROR",
                message="The pipeline ended without completing. No answer was produced.",
            )
        finally:
            if not (emitter.events and emitter.events[-1].is_terminal):
                emitter.emit(
                    EventName.ERROR,
                    code="INTERNAL_ERROR",
                    message="The pipeline ended without completing. No answer was produced.",
                )
            queue.put_nowait(f"data: {DONE}\n\n")

    async def frames() -> AsyncIterator[str]:
        task = asyncio.create_task(pipeline())
        try:
            while True:
                frame = await queue.get()
                yield frame
                if frame.startswith(f"data: {DONE}"):
                    break
        finally:
            # A client that hung up leaves the generator early. The pipeline task
            # is *not* cancelled: it writes the audit row, and FR-048 requires
            # that row whether or not anyone is listening.
            if not task.done():
                _log.debug(
                    "the client left the stream; the pipeline continues",
                    extra={"request_id": request_id},
                )

    return StreamingResponse(frames(), media_type=SSE_MEDIA_TYPE, headers=SSE_HEADERS)


__all__ = ["SSE_HEADERS", "SSE_MEDIA_TYPE", "ask_stream", "router"]
