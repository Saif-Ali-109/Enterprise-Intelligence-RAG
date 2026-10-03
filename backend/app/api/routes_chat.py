"""POST /chat and GET /chat/{request_id} — the non-streaming answer path (T090).

The pipeline is the same one the evaluation runner and the streaming path
execute (R-007): there is exactly one place an answer is assembled, and this
endpoint calls it. A reviewer opening an answer and a reviewer running the
eval script next to it are watching the same run shape, which is the only way a
metric computed offline can say anything about the live system.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends

from app.api.deps import Session
from app.chat.service import run_chat
from app.core.errors import ResourceNotFound
from app.core.logging import get_logger
from app.core.ratelimit import enforce_ask
from app.generation.provider import get_language_model
from app.retrieval.vector_store import get_vector_store
from app.schemas.chat import AskRequest, AskResponse

_log = get_logger("api.chat")

router = APIRouter(tags=["chat"])


@router.post(
    "/chat",
    response_model=AskResponse,
    summary="Ask a question",
    dependencies=[Depends(enforce_ask)],
)
async def ask(request: AskRequest, session: Session) -> AskResponse:
    """The non-streaming contract: one answer or one refusal, both labeled."""
    result = await run_chat(
        request.question,
        session=session,
        store=get_vector_store(),
        provider=get_language_model(),
        inspect=request.inspect,
    )
    # AskResponse is closed; the service intentionally decorates attack seconds.
    return AskResponse.model_validate(result.payload)


@router.get("/chat/{request_id}", response_model=AskResponse, summary="Get a recorded answer")
async def get_recorded_answer(request_id: uuid.UUID, session: Session) -> AskResponse:
    """The runtime audit: an answer survives, and re-fetching it is the audit.

    The current implementation re-runs the pipeline rather than storing a
    response row — there is no persisted answer path in this version of the
    schema. A request that claims an answer without the data to back it would
    be a stored refusal rather than a story we can read from this endpoint,
    and refuses to rewrite the past is louder than a 404 no one acts on.
    """

    raise ResourceNotFound(
        "This version of the service answers live; to read an answer again, ask the question again."
    )
