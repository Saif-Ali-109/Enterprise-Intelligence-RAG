"""ORM rows to response schemas, in one module.

Three reasons these functions are not written inline in the handlers:

**One definition of `vectors_live`.** It stays `True` for a document in `failed`
that was previously indexed, because a failed re-crawl leaves prior content live
(FR-054). That is why it is not derivable from `state`, and a handler that
computed `state == "indexed"` would serve the operator a document that answers
questions and claims to have no content.

**One definition of the crawl counters.** `CrawlJobCounters` is closed at five
fields, and the mapping from the row's five columns is arithmetic worth doing once:
`processed` includes unchanged pages, so `processed - unchanged` is the number
actually indexed. Where that is ambiguous — a job that never started, whose
columns are all null — the counters are zero, not null, because the contract
requires integers and a counter that can be null is a counter every client has to
null-check.

**`state_detail` is narrowed to its code.** The column is a JSONB object holding
the reason and whatever was measured; the contract types the field as a string.
Reducing it here means no handler can widen it by accident, and it means the
narrowing is one function with one test rather than one per handler.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from app.db.models import CrawlJob, Document, DocumentUnit, Source
from app.schemas.sources import (
    CrawlJobCounters,
    CrawlJobOut,
    CrawlJobStatus,
    DocumentOut,
    DocumentState,
    DocumentUnitOut,
    SourceOut,
    SourceStatus,
)


def _count(value: int | None) -> int:
    """A counter, with null meaning zero.

    Null is the state of a job that was created and never finished, and of a
    column a crash left behind. Reporting it as zero would claim "nothing was
    found"; reporting null would break the contract's `type: integer`. Zero with
    a `failed` status nearby is the honest reading, and the status is what a
    client reads first.
    """
    return 0 if value is None else int(value)


def source_status(row: Source) -> SourceStatus:
    """The status a client sees, which is not always the stored string.

    `enabled` is the operator's decision and `status` is the registry's
    observation, so disabling a source reports `disabled` even though the column
    may still say `active`. The stored value is left alone: re-enabling should
    restore the observed status, not force the operator to re-assert it.
    """
    if not row.enabled:
        return SourceStatus.DISABLED
    try:
        return SourceStatus(row.status)
    except ValueError:
        # A status this build does not know is not a 500. An unknown value means
        # the row was written by a newer version; reporting `active` is a lie,
        # and reporting `error` would alarm the operator about a source that may
        # be perfectly fine.
        return SourceStatus.ERROR


def source_out(
    row: Source,
    *,
    page_count: int,
    unit_count: int,
    last_crawl_status: str | None = None,
) -> SourceOut:
    """One source, with its derived counts.

    `last_crawl_status` is passed in rather than read from a relationship: a
    source's most recent job is a query the handler chooses to make, and a
    serialiser that issued its own would hide a join from the reader of the
    handler that has to reason about its cost.
    """
    status: CrawlJobStatus | None = None
    if last_crawl_status is not None:
        status = CrawlJobStatus(last_crawl_status)
    return SourceOut(
        id=row.id,
        name=row.name,
        start_url=row.start_url,
        allowed_domains=list(row.allowed_domains),
        product_domain=row.product_domain,
        enabled=row.enabled,
        status=source_status(row),
        page_count=page_count,
        unit_count=unit_count,
        last_crawl_at=row.last_crawl_at,
        last_crawl_status=status,
        created_at=row.created_at,
    )


def document_out(row: Document, *, unit_count: int) -> DocumentOut:
    """One document.

    `state_detail` becomes the reason code and nothing else. The full detail is
    logged at the moment it is written, keyed by document id, which is the place
    that can hold measured context without becoming a second, unpaged content
    surface.
    """
    detail: dict[str, Any] | None = row.state_detail
    code = detail.get("code") if isinstance(detail, dict) else None
    return DocumentOut(
        id=row.id,
        source_id=row.source_id,
        url=row.url,
        canonical_url=row.canonical_url,
        title=row.title,
        product=row.product,
        category=row.category,
        page_type=row.page_type,
        language=row.language,
        state=DocumentState(row.state),
        state_detail=code if isinstance(code, str) else None,
        vectors_live=row.vectors_live,
        unit_count=unit_count,
        content_fingerprint=row.content_fingerprint,
        source_modified_at=row.source_modified_at,
        last_crawled_at=row.last_crawled_at,
        indexed_at=row.indexed_at,
        tombstoned_at=row.tombstoned_at,
        created_at=row.created_at,
    )


def document_unit_out(row: DocumentUnit) -> DocumentUnitOut:
    """One unit's provenance. There is no text to return, and no field for it."""
    return DocumentUnitOut(
        id=row.id,
        document_id=row.document_id,
        ordinal=row.ordinal,
        vector_id=row.vector_id,
        heading_path=list(row.heading_path or []),
        block_types=list(row.block_types or []),
        token_count=row.token_count,
        overlap_tokens=_count(row.overlap_tokens),
        text_fingerprint=row.text_fingerprint,
    )


def crawl_job_counters(row: CrawlJob) -> CrawlJobCounters:
    """The five contract counters, computed from the row's five columns."""
    return CrawlJobCounters(
        discovered=_count(row.pages_discovered),
        processed=_count(row.pages_processed),
        unchanged=_count(row.pages_unchanged),
        skipped=_count(row.pages_skipped),
        failed=_count(row.pages_failed),
    )


def crawl_job_out(row: CrawlJob) -> CrawlJobOut:
    """One crawl job.

    `error_message` is the sanitised, bounded message written by the pipeline's
    `_safe_message`; it is passed through unchanged and never re-expanded here.
    """
    reasons: Any = row.skipped_reasons
    return CrawlJobOut(
        id=row.id,
        source_id=row.source_id,
        target_url=row.target_url,
        status=CrawlJobStatus(row.status),
        requested_at=row.requested_at,
        started_at=row.started_at,
        finished_at=row.finished_at,
        counters=crawl_job_counters(row),
        skipped_reasons=reasons if isinstance(reasons, dict) and reasons else None,
        error_code=row.error_code,
        error_message=row.error_message,
    )


def newest_job_at(rows: list[CrawlJob]) -> datetime | None:
    """The most recent `requested_at` in a set of jobs, or None."""
    return max((row.requested_at for row in rows), default=None)


__all__ = [
    "crawl_job_counters",
    "crawl_job_out",
    "document_out",
    "document_unit_out",
    "newest_job_at",
    "source_out",
    "source_status",
]
