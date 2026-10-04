"""Source registration, inspection, and crawl triggering.

The endpoints an operator uses to point the system at a documentation site.
Three of them decide something that matters more than the response they return,
and each decision is made before any connection is opened:

**Registration validates the address, then derives the scope from it.**
`allowed_domains` comes from the registrant's own URL, never from the request
body (data-model.md §2.1). A client that could supply the allowlist would be
supplying the thing the guard checks, which is the same as no guard.

**Registration returns the job that will do the crawl, committed first.** The row
is written and committed before the response is serialised, so the job id in the
body is an id a client can immediately poll — not a promise about a row that does
not exist yet. When the request sets `start_crawl: false` there is no job and the
field is `null`, which the contract now says explicitly.

**Re-registering a `start_url` returns the existing source with `200`.** Idempotent
by `start_url`: a double registration that silently created a second source would
crawl the same pages twice and report two corpora for one site.

`DELETE` removes registry rows *and* indexed vectors, tombstoning each document
before its vectors go (FR-031, FR-056), and refuses while a crawl is in flight —
deleting the source would cascade the running job row away underneath the task
that is still writing pages.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query, Response, status
from fastapi.responses import JSONResponse
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import DEFAULT_LIMIT, Session
from app.api.serialisers import crawl_job_out, source_out
from app.core.config import get_settings
from app.core.errors import InvalidUrl, ResourceNotFound, StateConflict, ValidationFailed
from app.core.logging import get_logger
from app.core.ratelimit import enforce_general
from app.core.security import assert_fetchable
from app.db.models import CrawlJob, Document, DocumentUnit, Source
from app.ingestion.crawl_service import start_and_schedule
from app.ingestion.pipeline import active_job_for, delete_document
from app.retrieval.vector_store import get_vector_store
from app.schemas.sources import (
    CrawlJobOut,
    SourceCreate,
    SourceOut,
    SourcePage,
    SourceRegistered,
    SourceUpdate,
)

router = APIRouter(tags=["sources"])

_log = get_logger("app.api.sources")


# ============================================================================
# Queries
# ============================================================================


async def _counts(
    session: AsyncSession, source_ids: list[uuid.UUID]
) -> dict[uuid.UUID, tuple[int, int]]:
    """Live documents and units per source, in two grouped queries.

    Tombstoned documents are excluded from both. A deleted document is still a
    row, and a count that includes it reports a corpus larger than the one that
    can actually be retrieved from — so the number an operator checks to confirm
    the system is working would report a contradiction instead.
    """
    if not source_ids:
        return {}

    documents = await session.execute(
        select(Document.source_id, func.count(Document.id))
        .where(Document.source_id.in_(source_ids), Document.state != "deleted")
        .group_by(Document.source_id)
    )
    units = await session.execute(
        select(Document.source_id, func.count(DocumentUnit.id))
        .join(Document, Document.id == DocumentUnit.document_id)
        .where(Document.source_id.in_(source_ids), Document.state != "deleted")
        .group_by(Document.source_id)
    )
    pages: dict[uuid.UUID, int] = dict(documents.all())  # type: ignore[arg-type]
    chunks: dict[uuid.UUID, int] = dict(units.all())  # type: ignore[arg-type]
    return {
        source_id: (int(pages.get(source_id, 0)), int(chunks.get(source_id, 0)))
        for source_id in source_ids
    }


async def _latest_jobs(
    session: AsyncSession, source_ids: list[uuid.UUID]
) -> dict[uuid.UUID, CrawlJob]:
    """The most recent job per source, keyed by source id.

    One ordered query, keeping the first row seen per source. `id` breaks ties so
    the answer does not depend on two jobs sharing a timestamp to the microsecond,
    which `TIMESTAMPTZ` on PostgreSQL does allow.
    """
    if not source_ids:
        return {}
    result = await session.execute(
        select(CrawlJob)
        .where(CrawlJob.source_id.in_(source_ids))
        .order_by(CrawlJob.requested_at.desc(), CrawlJob.id.desc())
    )
    latest: dict[uuid.UUID, CrawlJob] = {}
    for job in result.scalars():
        latest.setdefault(job.source_id, job)
    return latest


async def _render(session: AsyncSession, rows: list[Source]) -> list[SourceOut]:
    """Serialise sources with their derived counts and last-crawl status."""
    ids = [row.id for row in rows]
    counts = await _counts(session, ids)
    jobs = await _latest_jobs(session, ids)
    rendered = []
    for row in rows:
        page_count, unit_count = counts.get(row.id, (0, 0))
        job = jobs.get(row.id)
        rendered.append(
            source_out(
                row,
                page_count=page_count,
                unit_count=unit_count,
                last_crawl_status=job.status if job is not None else None,
            )
        )
    return rendered


async def _load_source(session: AsyncSession, source_id: uuid.UUID) -> Source:
    """Fetch a source or raise `NOT_FOUND`.

    The id goes in `details`, never interpolated into the message: the message
    stays authored so a client can match on it, and an opaque id is a value
    `details` is documented to carry.
    """
    row = await session.get(Source, source_id)
    if row is None:
        raise ResourceNotFound("No such source.", details={"source_id": str(source_id)})
    return row


# ============================================================================
# Routes
# ============================================================================


@router.get(
    "/sources",
    response_model=SourcePage,
    summary="List registered sources",
    description=(
        "Registered sources with live page and unit counts. Tombstoned documents are excluded "
        "from both counts. `last_crawl_status` is the most recent job's status, and is null for a "
        "source that has never been crawled — which is not the same as a source in `error`."
    ),
    dependencies=[Depends(enforce_general)],
)
async def list_sources(
    session: Session,
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    enabled: bool | None = Query(default=None),
) -> SourcePage:
    """Every registered source, newest first, in the contract's envelope."""
    statement = select(Source).order_by(Source.created_at.desc(), Source.id.desc())
    if enabled is not None:
        statement = statement.where(Source.enabled.is_(enabled))

    # The total is counted with the same filter as the rows. Counting all sources
    # while returning a filtered page is the kind of mismatch a client only
    # notices when its last page is empty and the count still says there are more.
    counting = select(func.count(Source.id))
    if enabled is not None:
        counting = counting.where(Source.enabled.is_(enabled))
    total = int((await session.execute(counting)).scalar_one())

    result = await session.execute(statement.limit(limit).offset(offset))
    rows = list(result.scalars())
    return SourcePage(items=await _render(session, rows), total=total, limit=limit, offset=offset)


@router.post(
    "/sources",
    response_model=SourceRegistered,
    status_code=status.HTTP_201_CREATED,
    summary="Register a source and start its first crawl",
    description=(
        "The URL is checked against the address guard **before any connection is opened** "
        "(FR-044). A disallowed address is rejected, never fetched. `allowed_domains` is derived "
        "from the supplied URL, never taken from the request body. Re-registering an existing "
        "`start_url` returns `200` with that source and starts no second crawl."
    ),
    dependencies=[Depends(enforce_general)],
)
async def register_source(
    payload: SourceCreate, session: Session
) -> SourceRegistered | JSONResponse:
    """Register a source, then crawl it — unless the caller said not to."""
    settings = get_settings()
    start_url = str(payload.start_url)

    # The guard, before anything else. This raises `SsrfBlocked` or `InvalidUrl`
    # and performs the DNS resolution, so the host is in the log and never in the
    # response: naming a refused host confirms to whoever probed which internal
    # addresses are in use, which is the information the guard exists to withhold.
    check = assert_fetchable(start_url, allowed_domains=settings.allowed_domains)
    if check.host is None:  # pragma: no cover - `assert_fetchable` returns ok only with a host
        raise InvalidUrl("The URL is not a parseable absolute http or https address.")

    existing = (
        (await session.execute(select(Source).where(Source.start_url == start_url)))
        .scalars()
        .first()
    )
    if existing is not None:
        rendered = (await _render(session, [existing]))[0]
        return JSONResponse(
            status_code=status.HTTP_200_OK, content=rendered.model_dump(mode="json")
        )

    source = Source(
        name=payload.name,
        start_url=start_url,
        allowed_domains=[check.host],
        product_domain=payload.product_domain,
        max_pages=payload.max_pages,
        max_depth=payload.max_depth,
        delay_seconds=payload.delay_seconds,
    )
    session.add(source)
    try:
        await session.flush()
    except IntegrityError:
        # Two registrations of the same URL raced; the unique constraint is the
        # authority and the loser re-reads rather than reporting a conflict.
        await session.rollback()
        row = (
            (await session.execute(select(Source).where(Source.start_url == start_url)))
            .scalars()
            .first()
        )
        if row is None:  # pragma: no cover - the constraint fired on something else
            raise ValidationFailed("The source could not be registered.") from None
        rendered = (await _render(session, [row]))[0]
        return JSONResponse(
            status_code=status.HTTP_200_OK, content=rendered.model_dump(mode="json")
        )

    job: CrawlJobOut | None = None
    if payload.start_crawl:
        created = await start_and_schedule(session, source=source)
        job = crawl_job_out(created)

    rendered = (await _render(session, [source]))[0]
    await session.commit()
    _log.info(
        "source registered",
        extra={"source_id": str(source.id), "crawl_started": job is not None},
    )
    return SourceRegistered(source=rendered, crawl_job=job)


@router.get(
    "/sources/{source_id}",
    response_model=SourceOut,
    summary="Retrieve a source",
    dependencies=[Depends(enforce_general)],
)
async def get_source(source_id: uuid.UUID, session: Session) -> SourceOut:
    """One source, with its counts and its last crawl's status."""
    source = await _load_source(session, source_id)
    return (await _render(session, [source]))[0]


@router.patch(
    "/sources/{source_id}",
    response_model=SourceOut,
    summary="Update a source, including enabling or disabling it",
    description=(
        "Every field is optional and only the supplied fields change. `product_domain` may be set "
        'to `null` explicitly, which is how a client says "unknown": FR-009 forbids forcing a '
        "classification, and omitting the field is a different request from clearing it. The "
        "reported `status` is `disabled` whenever `enabled` is false, whatever the stored value."
    ),
    dependencies=[Depends(enforce_general)],
)
async def update_source(source_id: uuid.UUID, payload: SourceUpdate, session: Session) -> SourceOut:
    """Apply a partial update.

    `exclude_unset`, not the field values: absent must mean "leave alone", and a
    model read directly would clear a classification every time a client renamed a
    source. The stored `status` is deliberately not rewritten — it is the
    registry's observation, and re-enabling a source should restore what was
    observed rather than assert a fresh `active`.
    """
    source = await _load_source(session, source_id)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(source, field, value)
    await session.commit()
    return (await _render(session, [source]))[0]


@router.delete(
    "/sources/{source_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove a source and all of its content",
    description=(
        "Removes the registry rows **and** the indexed vectors, so the content can no longer be "
        "retrieved (FR-031). Each document is tombstoned before its vectors are deleted, so an "
        "in-flight crawl cannot resurrect it (FR-056). Refused with `409` while a crawl is in "
        "progress, because removing the source would cascade the running job row away underneath "
        "the task still writing pages."
    ),
    dependencies=[Depends(enforce_general)],
)
async def remove_source(source_id: uuid.UUID, session: Session) -> Response:
    """Remove a source and everything it indexed."""
    await _load_source(session, source_id)

    running = await active_job_for(session, source_id)
    if running is not None:
        raise StateConflict(
            "This source has a crawl in progress; wait for it to finish before removing it.",
            details={"source_id": str(source_id), "running_job_id": str(running.id)},
        )

    documents = list(
        (await session.execute(select(Document).where(Document.source_id == source_id)))
        .scalars()
        .all()
    )
    store = get_vector_store()
    for document in documents:
        # One transaction per document. Vector deletion is not transactional, so a
        # single transaction for the lot would roll back the registry half of a
        # removal whose vector half already happened — leaving vectors the
        # registry no longer accounts for. Per document, a failure leaves the
        # documents already removed removed and the rest still listed, which an
        # operator can see and finish.
        await delete_document(session, store, document_id=document.id)
        await session.commit()

    await session.execute(delete(Source).where(Source.id == source_id))
    await session.commit()
    _log.info(
        "source removed",
        extra={"source_id": str(source_id), "documents": len(documents)},
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/sources/{source_id}/crawl",
    response_model=CrawlJobOut,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Request a re-crawl of a source",
    description=(
        "Accepts the crawl and returns the job that will do it. A second request while a crawl is "
        "in progress is refused with `CRAWL_ALREADY_RUNNING` and the running job's id (FR-055). A "
        "disabled source is refused with `VALIDATION_ERROR`: crawling it would contradict the "
        "operator's own instruction."
    ),
    dependencies=[Depends(enforce_general)],
)
async def crawl_registered_source(source_id: uuid.UUID, session: Session) -> CrawlJobOut:
    """Queue a re-crawl of a registered source."""
    source = await _load_source(session, source_id)
    if not source.enabled:
        raise ValidationFailed(
            "This source is disabled; enable it before crawling.",
            details={"source_id": str(source_id)},
        )
    job = await start_and_schedule(session, source=source)
    return crawl_job_out(job)


__all__ = ["router"]
