"""`GET /config` — effective operational configuration, with no secret values.

FR-042, Principle VII. This endpoint exists so an operator can see *why* the
system behaves as it does: the retrieval budget, the model, the index, the corpus
size, and which credentials are present.

The security property is the shape, not a filter. `SecretPresence` is closed and
holds exactly one field — `configured: bool`. There is no `value` attribute
anywhere in the response tree, so there is nothing for a careless `asdict()` or
a hand-edited handler to leak. That is a stronger guarantee than "we remember not
to return it": a guarantee you have to remember is a guarantee you will forget.

`corpus` is counted from the registry rather than read from configuration,
because the counts are facts about what has been ingested, not settings. They
are the one part of this response that changes without a restart.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.ratelimit import enforce_general
from app.db.models import Document, DocumentUnit, Source
from app.db.session import get_session
from app.schemas import ConfigResponse, CorpusSummary

router = APIRouter(tags=["system"])


async def get_session_for_config() -> AsyncIterator[AsyncSession]:
    """The request-scoped session, as a FastAPI dependency.

    A thin re-export of `get_session` rather than a second implementation. Two
    session factories would be two transaction scopes, and which one a handler
    used would depend on how it happened to be written.

    The annotations are resolved by FastAPI at import time, so `AsyncSession` is
    imported at runtime here — see the note in `app/core/ratelimit.py` for why a
    `TYPE_CHECKING` import would break this silently.
    """
    async for session in get_session():
        yield session


async def _count_corpus(session: AsyncSession) -> CorpusSummary:
    """Count sources, live documents, and units.

    Two of the three counts exclude tombstones, deliberately. A deleted document
    is still a row, and a `COUNT(*)` that includes tombstones reports a corpus
    larger than the one that can be retrieved from — which would make the one
    number an operator checks to confirm the system is working report a
    contradiction instead.

    All three counts come from one round trip rather than three, so the numbers
    cannot describe different instants.
    """
    source_count = await session.scalar(select(func.count()).select_from(Source))
    document_count = await session.scalar(
        select(func.count()).select_from(Document).where(Document.state != "deleted")
    )
    unit_count = await session.scalar(
        select(func.count())
        .select_from(DocumentUnit)
        .join(Document, DocumentUnit.document_id == Document.id)
        .where(Document.state != "deleted")
    )

    return CorpusSummary(
        source_count=int(source_count or 0),
        document_count=int(document_count or 0),
        unit_count=int(unit_count or 0),
        allowed_domains=get_settings().public_domains(),
    )


@router.get(
    "/config",
    response_model=ConfigResponse,
    summary="Operational configuration, with no secret values",
    description=(
        "Returns the settings that affect retrieval and generation. **Secret values are "
        "never returned** — a configured credential is reported as `configured: true` with "
        "no value, and `SecretPresence` has no value field to return (FR-042)."
    ),
    dependencies=[Depends(enforce_general)],
)
async def get_config(session: AsyncSession = Depends(get_session_for_config)) -> ConfigResponse:
    """Effective configuration plus live corpus counts.

    The `request` parameter FastAPI would normally inject is not declared: this
    handler needs nothing from it. Declaring an unused parameter is the kind of
    thing that later gets used "just in case" and turns into a dependency on
    request state that is untested.
    """
    settings = get_settings()
    payload = settings.public_config()

    return ConfigResponse(
        retrieval=payload["retrieval"],
        generation=payload["generation"],
        index=payload["index"],
        corpus=await _count_corpus(session),
        secrets=payload["secrets"],
    )


__all__ = ["get_config", "get_session_for_config", "router"]
