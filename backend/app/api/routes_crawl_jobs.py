"""Reading crawl jobs: what was requested, what happened, and what was refused.

A crawl job is the only record of *why* a corpus looks the way it does, so these
endpoints are read-only by design — nothing here changes a job's state. A status
transition is the pipeline's decision, made once, with its counts; an endpoint
that could move a job to `completed` would let a client write a corpus report
that never happened.

Two things the serialiser has to get right, and both are in
`app/api/serialisers.py` rather than here:

**Counters are five, and null is zero.** The contract closes `counters` at five
fields, and a job that was created and never started has null columns. Reporting
null would break the declared type; reporting zero is only honest alongside the
`failed` or `running` status, which is the first field a client reads.

**`skipped_reasons` is a flat reason-to-count map.** It is the answer to "why did
the crawler not read the pages it discovered?" — `robots_disallowed: 3`,
`duplicate: 12`. A null when there were no skips, so an empty object and a
missing breakdown are not confused: one means "nothing was skipped", the other
means "this job never reported".
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select

from app.api.deps import DEFAULT_LIMIT, Session
from app.api.serialisers import crawl_job_out
from app.core.errors import ResourceNotFound
from app.core.ratelimit import enforce_general
from app.db.models import CrawlJob
from app.schemas.sources import CrawlJobOut, CrawlJobPage, CrawlJobStatus

router = APIRouter(tags=["crawl-jobs"])


@router.get(
    "/crawl-jobs",
    response_model=CrawlJobPage,
    summary="List crawl jobs",
    description=(
        "Newest first, filtered by source and status. Counters are the five the contract "
        "declares; `indexed` is `processed` minus `unchanged`. `skipped_reasons` is a flat map "
        "of reason to page count, and answers why a crawl read fewer pages than it discovered."
    ),
    dependencies=[Depends(enforce_general)],
)
async def list_crawl_jobs(
    session: Session,
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    source_id: uuid.UUID | None = Query(default=None),
    status_filter: CrawlJobStatus | None = Query(default=None, alias="status"),
) -> CrawlJobPage:
    """One page of jobs, newest first.

    The status query parameter is aliased to `status` because the contract names
    it `status`; a parameter called `status_filter` in the signature would leak
    into the generated OpenAPI as `status_filter` and the contract suite's
    path/method agreement would not catch the query-name drift — only a client
    would, by sending the documented name and getting a 200 with the wrong filter.
    """
    conditions = []
    if source_id is not None:
        conditions.append(CrawlJob.source_id == source_id)
    if status_filter is not None:
        conditions.append(CrawlJob.status == status_filter.value)

    counting = select(func.count(CrawlJob.id))
    listing = select(CrawlJob)
    for condition in conditions:
        counting = counting.where(condition)
        listing = listing.where(condition)

    total = int((await session.execute(counting)).scalar_one())
    result = await session.execute(
        listing.order_by(CrawlJob.requested_at.desc(), CrawlJob.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return CrawlJobPage(
        items=[crawl_job_out(row) for row in result.scalars()],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/crawl-jobs/{job_id}",
    response_model=CrawlJobOut,
    summary="Retrieve a crawl job",
    description=(
        "The full record of one crawl, including per-page counters, why pages were skipped, and "
        "the error code and bounded message when the job itself failed (FR-047, FR-054)."
    ),
    dependencies=[Depends(enforce_general)],
)
async def get_crawl_job(job_id: uuid.UUID, session: Session) -> CrawlJobOut:
    """One crawl job."""
    row = await session.get(CrawlJob, job_id)
    if row is None:
        raise ResourceNotFound("No such crawl job.", details={"crawl_job_id": str(job_id)})
    return crawl_job_out(row)


__all__ = ["router"]
