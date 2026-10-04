"""Browsing the corpus: documents, their units, and deletion.

This is the operator's window onto what was ingested, and the shape of that
window is a deliberate boundary. The contract returns **provenance without text**:
a document's units come back with heading paths, ordinals, token counts, block
types, and fingerprints, but no unit text — the text lives in the vector store,
and an endpoint that returned it would make the corpus view a bulk content export
behind a read-only permission (FR-031). FR-024 is what makes the window useful:
a citation's heading path and ordinal resolve to exactly one unit, so an operator
can confirm *why* a citation points at a passage without the passage being served
by a second route.

Three further decisions:

**A deleted document is still listable.** `state` and `tombstoned_at` say so.
Hiding tombstones would make a corpus view that quietly shrinks, and the operator
would have no way to tell a page that was removed from one that was never
crawled — two very different events that look identical as an absence.

**Filters are exact matches.** `state`, `product`, and `category` are equality, not
substrings. A substring search over classification is how "jira" starts matching
"jira-software-cloud-docs", and every caller then has to know which convention the
matcher used.

**Deletion is 204 and idempotent at the document level.** A second `DELETE` on an
already-deleted document succeeds rather than 404-ing, because the caller's
intent — "this content must not be retrievable" — is already satisfied, and a 404
would tell them to retry something that is already true.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import DEFAULT_LIMIT, Session
from app.api.serialisers import document_out, document_unit_out
from app.core.errors import ResourceNotFound
from app.core.logging import get_logger
from app.core.ratelimit import enforce_general
from app.db.models import Document, DocumentUnit
from app.ingestion.pipeline import delete_document
from app.retrieval.vector_store import get_vector_store
from app.schemas.sources import (
    DocumentOut,
    DocumentPage,
    DocumentState,
    DocumentUnitOut,
    DocumentUnitPage,
)

router = APIRouter(tags=["documents"])

_log = get_logger("app.api.documents")


async def _unit_counts(
    session: AsyncSession, document_ids: list[uuid.UUID]
) -> dict[uuid.UUID, int]:
    """Units per document, in one grouped query.

    `documents.unit_count` exists and is maintained by the same transaction that
    writes the units, so this is a cross-check rather than the source of truth. It
    is the safer of the two to *report*: a count read from the units cannot
    disagree with the units a caller is about to page through, and the two are
    only ever different when a transaction is mid-flight.
    """
    if not document_ids:
        return {}
    result = await session.execute(
        select(DocumentUnit.document_id, func.count(DocumentUnit.id))
        .where(DocumentUnit.document_id.in_(document_ids))
        .group_by(DocumentUnit.document_id)
    )
    return {document_id: int(count) for document_id, count in result.all()}


async def _render_documents(session: AsyncSession, rows: list[Document]) -> list[DocumentOut]:
    counts = await _unit_counts(session, [row.id for row in rows])
    return [document_out(row, unit_count=counts.get(row.id, 0)) for row in rows]


async def _load_document(session: AsyncSession, document_id: uuid.UUID) -> Document:
    row = await session.get(Document, document_id)
    if row is None:
        raise ResourceNotFound("No such document.", details={"document_id": str(document_id)})
    return row


# ============================================================================
# Routes
# ============================================================================


@router.get(
    "/documents",
    response_model=DocumentPage,
    summary="Browse indexed documents",
    description=(
        "Filtered by source, state, product, and category. Matching is exact, never substring. "
        "Tombstoned documents are listed: their state is `deleted` and `tombstoned_at` says "
        "when, because a corpus view that silently shrinks cannot be told apart from one that "
        "was never crawled."
    ),
    dependencies=[Depends(enforce_general)],
)
async def list_documents(
    session: Session,
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    source_id: uuid.UUID | None = Query(default=None),
    state: DocumentState | None = Query(default=None),
    product: str | None = Query(default=None),
    category: str | None = Query(default=None),
) -> DocumentPage:
    """One page of documents, with the total for the same filter."""
    conditions = []
    if source_id is not None:
        conditions.append(Document.source_id == source_id)
    if state is not None:
        conditions.append(Document.state == state.value)
    if product is not None:
        conditions.append(Document.product == product)
    if category is not None:
        conditions.append(Document.category == category)

    counting = select(func.count(Document.id))
    listing = select(Document)
    for condition in conditions:
        counting = counting.where(condition)
        listing = listing.where(condition)

    total = int((await session.execute(counting)).scalar_one())
    # Newest first, with the id breaking ties: two pages crawled in the same
    # millisecond must not swap places between calls, or a client scrolling with
    # `offset` silently skips and repeats rows.
    result = await session.execute(
        listing.order_by(Document.created_at.desc(), Document.id.desc()).limit(limit).offset(offset)
    )
    rows = list(result.scalars())
    return DocumentPage(
        items=await _render_documents(session, rows), total=total, limit=limit, offset=offset
    )


@router.get(
    "/documents/{document_id}",
    response_model=DocumentOut,
    summary="Retrieve a document",
    dependencies=[Depends(enforce_general)],
)
async def get_document_route(document_id: uuid.UUID, session: Session) -> DocumentOut:
    """One document."""
    document = await _load_document(session, document_id)
    counts = await _unit_counts(session, [document.id])
    return document_out(document, unit_count=counts.get(document.id, 0))


@router.delete(
    "/documents/{document_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a document and remove its indexed content",
    description=(
        "Tombstones the document, then removes its vectors, so the content is unreachable in "
        "subsequent answers (FR-031, FR-056). Idempotent."
    ),
    dependencies=[Depends(enforce_general)],
)
async def delete_document_route(document_id: uuid.UUID, session: Session) -> Response:
    """Delete one document and its vectors, in one transaction."""
    await _load_document(session, document_id)
    result = await delete_document(session, get_vector_store(), document_id=document_id)
    await session.commit()
    _log.info(
        "document deleted via api",
        extra={
            "document_id": str(document_id),
            "units_removed": result.units_removed,
            "already_deleted": result.already_deleted,
        },
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/documents/{document_id}/units",
    response_model=DocumentUnitPage,
    summary="List a document's retrievable units",
    description=(
        "Provenance for each unit — heading path, ordinal, token count, overlap, block types, "
        "fingerprint — in ordinal order. **Unit text is not returned**; it is held in the vector "
        "store. This is the view that lets an operator confirm structure-aware chunking without a "
        "content dump."
    ),
    dependencies=[Depends(enforce_general)],
)
async def list_document_units(
    document_id: uuid.UUID,
    session: Session,
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> DocumentUnitPage:
    """A document's units, ordered by ordinal.

    Ordered by ordinal rather than created time because ordinal *is* the reading
    order, and it is the order a citation's heading path is resolved in (FR-024).
    """
    await _load_document(session, document_id)

    total = int(
        (
            await session.execute(
                select(func.count(DocumentUnit.id)).where(DocumentUnit.document_id == document_id)
            )
        ).scalar_one()
    )
    result = await session.execute(
        select(DocumentUnit)
        .where(DocumentUnit.document_id == document_id)
        .order_by(DocumentUnit.ordinal)
        .limit(limit)
        .offset(offset)
    )
    items: list[DocumentUnitOut] = [document_unit_out(row) for row in result.scalars()]
    return DocumentUnitPage(items=items, total=total, limit=limit, offset=offset)


__all__ = ["router"]
