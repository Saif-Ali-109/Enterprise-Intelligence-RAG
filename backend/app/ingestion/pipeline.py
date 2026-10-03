"""Indexing, registry persistence, supersession, the crawl orchestrator, deletion.

T058, T059, T060, T061, T062. FR-025, FR-026, FR-027, FR-031, FR-053, FR-054,
FR-055, FR-056, SC-012, SC-013, data-model.md §3.

One module holds all five because they are one transaction's worth of
decisions, and splitting them is how the invariants below stop being
invariants:

- **The only place vectors are written** is `index_units` (stage 9).
- **The only place vectors are deleted** is `delete_document` (stage 11), and
  it runs only on transition to `deleted` (data-model §3.2).
- **The tombstone is checked before any write**, so an in-flight crawl cannot
  resurrect deleted content (FR-056).
- **A failed page never aborts the run**; it becomes a count and a reason on
  the crawl job (FR-054).

Every function that touches the database takes an explicit `AsyncSession` and
every function that touches the vector store takes an explicit `VectorStore`.
Neither is constructed here. That is what lets the whole module be tested
against a fake store and a real Postgres without a network, and it is why
"which store did this write to?" is answerable by reading a call site.

`vector_id` is `{document_id}#{ordinal:04d}` and is **immutable** (R-005).
Supersession therefore upserts over the same id rather than deleting and
re-inserting: a new version of ordinal 7 reuses its predecessor's vector id,
so a citation that survived a re-crawl keeps resolving to a live vector.
Only ordinals that no longer exist are orphans, and only they are deleted.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete as sa_delete
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import CrawlAlreadyRunning, DocumentDeleted, RobotsDisallowed, StateConflict
from app.core.logging import get_logger
from app.db.models import CrawlJob, Document, DocumentUnit, Source
from app.ingestion.chunker import Chunk, ChunkResult, overlap_tokens
from app.ingestion.fingerprint import UNCHANGED, classify_change, unit_text_fingerprint
from app.ingestion.metadata import PageMetadata
from app.ingestion.page_pipeline import IngestionRejected, PageIngestion, ingest_page
from app.retrieval.vector_store import BatchResult, VectorRecord, VectorStore

_log = get_logger("ingestion.pipeline")


def _utcnow() -> datetime:
    return datetime.now(UTC)


def vector_id_for(document_id: uuid.UUID, ordinal: int) -> str:
    """R-005's id scheme. Immutable for the life of a document."""
    return f"{document_id}#{ordinal:04d}"


# ============================================================================
# Stage 9 — indexing (T058)
# ============================================================================


def unit_metadata(
    *,
    document_id: uuid.UUID,
    source_id: uuid.UUID,
    url: str,
    metadata: PageMetadata,
    chunk: Chunk,
    content_fingerprint: str,
    indexed_at: datetime,
) -> dict[str, Any]:
    """The flat, filterable denormalisation from data-model.md §5.

    Flat because Pinecone's filter language has no nested object and a nested
    metadata silently fails to match — a filter that looks configured and is
    not (T027's measured finding). The dimension is never referenced here: the
    store embeds, so a model change is the store's business (R-003).

    `namespace` is deliberately absent: it is reserved by the store and
    appearing in metadata is a mistake that fails at write time.
    """
    return {
        "document_id": str(document_id),
        "source_id": str(source_id),
        "product": metadata.product,
        "category": metadata.category,
        "page_type": metadata.page_type,
        "language": metadata.language,
        "heading_path": list(chunk.heading_path),
        "title": metadata.title,
        "source_url": url,
        "ordinal": chunk.ordinal,
        "content_fingerprint": content_fingerprint,
        "indexed_at": indexed_at.isoformat(),
    }


async def index_units(
    store: VectorStore,
    *,
    document_id: uuid.UUID,
    source_id: uuid.UUID,
    url: str,
    metadata: PageMetadata,
    chunks: Iterable[Chunk],
    content_fingerprint: str,
    indexed_at: datetime | None = None,
) -> BatchResult:
    """Write one unit's worth of vectors. The only vector *write* path.

    Returns the store's own `BatchResult`, because `submitted` and `written`
    differ whenever the service drops a record and a caller that trusts its
    own count will later believe a chunk is retrievable when it is not.
    """
    stamped = indexed_at or _utcnow()
    records = [
        VectorRecord(
            id=vector_id_for(document_id, chunk.ordinal),
            text=chunk.text,
            metadata=unit_metadata(
                document_id=document_id,
                source_id=source_id,
                url=url,
                metadata=metadata,
                chunk=chunk,
                content_fingerprint=content_fingerprint,
                indexed_at=stamped,
            ),
        )
        for chunk in chunks
    ]
    if not records:
        return BatchResult(written=0, submitted=0)
    result = await store.upsert(records)
    _log.info(
        "units indexed",
        extra={
            "document_id": str(document_id),
            "submitted": result.submitted,
            "written": result.written,
        },
    )
    return result


# ============================================================================
# Supersession planning (T060)
# ============================================================================


@dataclass(frozen=True, slots=True)
class SupersessionPlan:
    """What a re-crawl must write, keep, and remove — decided before any write.

    Computed from the registry's existing units and the freshly chunked page,
    so the decision is testable with no store and no database, and so a caller
    can inspect the plan before it commits to it.
    """

    #: Ordinals whose text changed (or are new): their vectors are upserted.
    to_write: list[Chunk]
    #: Ordinals whose fingerprint is unchanged: no embed, no write, no churn.
    unchanged: list[int]
    #: Vector ids whose ordinal no longer exists on the new page.
    orphaned_vector_ids: list[str]
    #: Vector ids for superseded units — kept, because they are overwritten in
    #: place under the same immutable id.
    superseded_vector_ids: list[str]

    @property
    def writes_needed(self) -> bool:
        return bool(self.to_write or self.orphaned_vector_ids)


def plan_supersession(
    existing: dict[int, DocumentUnit],
    chunks: list[Chunk],
) -> SupersessionPlan:
    """Reconcile the stored units against the new page's chunks.

    Three outcomes, and only three:

    - same ordinal, same text fingerprint → **unchanged**. The vector stays.
      Re-embedding an identical unit costs money and changes nothing a reader
      can observe, and SC-012 is about not accumulating duplicates.
    - same ordinal, different fingerprint → **superseded**. Upserted under the
      same vector id (R-005), so nothing is deleted and a citation minted
      before the re-crawl keeps resolving.
    - ordinal beyond the new page's last → **orphaned**. Deleted, and its row
      removed. This is the only deletion a re-crawl performs, and it is
      deletion of *this document's* vectors on the transition to a new
      version — not the FR-031 deletion path, which is the only transition that
      removes the rest.
    """
    to_write: list[Chunk] = []
    unchanged: list[int] = []
    superseded: list[str] = []

    for chunk in chunks:
        prior = existing.get(chunk.ordinal)
        if prior is None:
            to_write.append(chunk)
            continue
        if prior.text_fingerprint == unit_text_fingerprint(chunk.text):
            unchanged.append(chunk.ordinal)
            continue
        to_write.append(chunk)
        superseded.append(prior.vector_id)

    live_ordinals = {chunk.ordinal for chunk in chunks}
    orphaned = [
        unit.vector_id for ordinal, unit in sorted(existing.items()) if ordinal not in live_ordinals
    ]

    return SupersessionPlan(
        to_write=to_write,
        unchanged=unchanged,
        orphaned_vector_ids=orphaned,
        superseded_vector_ids=superseded,
    )


# ============================================================================
# Stage 10 — registry persistence (T059)
# ============================================================================


@dataclass(frozen=True, slots=True)
class PersistResult:
    document: Document
    written: int
    unchanged: int
    deleted: int


def _transition(
    document: Document,
    *,
    state: str,
    vectors_live: bool | None = None,
) -> None:
    """Move a document's lifecycle state, refusing the one illegal move.

    `deleted` is terminal (data-model §3.1). A transition *out of* `deleted`
    is not "not currently happening" — it is the resurrection FR-056 forbids,
    and the tombstone check in the orchestrator is the primary guard. This is
    the second one, for the path that bypasses the orchestrator.
    """
    if document.state == "deleted" and state != "deleted":
        raise StateConflict(
            "a deleted document cannot return to a live state; its content was removed on request"
        )
    document.state = state
    if vectors_live is not None:
        document.vectors_live = vectors_live


async def get_document(session: AsyncSession, source_id: uuid.UUID, url: str) -> Document | None:
    result = await session.execute(
        select(Document).where(Document.source_id == source_id, Document.url == url)
    )
    return result.scalar_one_or_none()


async def get_document_for_update(
    session: AsyncSession, source_id: uuid.UUID, url: str
) -> Document | None:
    """The same row, locked for the duration of the transaction.

    `FOR UPDATE` because two crawls of *different* sources can legitimately
    reach the same canonical page, and both may then write units for it. The
    lock serialises them; the unique constraint on `(source_id, url)` alone
    would only have caught identical registrations.
    """
    result = await session.execute(
        select(Document)
        .where(Document.source_id == source_id, Document.url == url)
        .with_for_update()
    )
    return result.scalar_one_or_none()


async def register_document(
    session: AsyncSession,
    *,
    source_id: uuid.UUID,
    url: str,
) -> Document:
    """Get-or-create the `discovered` row for a URL. Idempotent (FR-025)."""
    existing = await get_document(session, source_id, url)
    if existing is not None:
        return existing
    document = Document(source_id=source_id, url=url, state="discovered", unit_count=0)
    session.add(document)
    await session.flush()
    return document


async def persist_indexed_document(
    session: AsyncSession,
    store: VectorStore,
    *,
    source_id: uuid.UUID,
    ingestion: PageIngestion,
    result: ChunkResult,
    indexed_at: datetime | None = None,
) -> PersistResult:
    """Write units, fingerprint, and the `indexed` transition in one place.

    Order is load-bearing and follows data-model §3.3: the new vectors and
    unit rows are written **before** the document is marked `indexed`, and an
    orphaned unit's vector is deleted only after its replacement is live. A
    failure part-way leaves the previous version live and the document
    `processed`, which is exactly the FR-054 guarantee.

    `processed` and `indexed` are distinct states and are used as such: the
    caller marks `processed` the moment extraction and chunking succeed, so a
    failure in the vector write is visible as "extracted but not indexed"
    rather than as a page that appears never to have been read (FR-053).
    """
    stamped = indexed_at or _utcnow()
    document = await get_document_for_update(session, source_id, ingestion.url)
    if document is None:
        document = await register_document(session, source_id=source_id, url=ingestion.url)

    if document.state == "deleted":
        # Checked here as well as in the orchestrator: this function is the one
        # that writes, so this is the last point at which resurrection can be
        # refused (FR-056).
        raise DocumentDeleted(
            f"{ingestion.url} was deleted; its content cannot be restored by a crawl"
        )

    existing = {unit.ordinal: unit for unit in (await _units_of(session, document.id))}
    plan = plan_supersession(existing, result.chunks)

    batch = await index_units(
        store,
        document_id=document.id,
        source_id=source_id,
        url=ingestion.url,
        metadata=ingestion.metadata,
        chunks=plan.to_write,
        content_fingerprint=ingestion.content_fingerprint,
        indexed_at=stamped,
    )

    # Orphans die only after their replacements are live.
    deleted = 0
    if plan.orphaned_vector_ids:
        deleted = await store.delete(plan.orphaned_vector_ids)
        await _drop_units(session, plan.orphaned_vector_ids)

    await _sync_units(session, document_id=document.id, chunks=result.chunks, plan=plan)

    document.title = ingestion.metadata.title
    document.canonical_url = ingestion.metadata.canonical_url
    document.product = ingestion.metadata.product
    document.category = ingestion.metadata.category
    document.page_type = ingestion.metadata.page_type
    document.language = ingestion.metadata.language
    document.content_fingerprint = ingestion.content_fingerprint
    document.last_crawled_at = stamped
    document.indexed_at = stamped
    document.unit_count = len(result.chunks)
    document.state_detail = None
    _transition(document, state="indexed", vectors_live=True)
    await session.flush()

    return PersistResult(
        document=document,
        written=batch.written,
        unchanged=len(plan.unchanged),
        deleted=deleted,
    )


async def mark_processed(
    session: AsyncSession,
    *,
    source_id: uuid.UUID,
    url: str,
    fingerprint: str,
    unit_count: int,
) -> Document | None:
    """Record that a page was extracted and chunked, with no vectors written."""
    document = await get_document_for_update(session, source_id, url)
    if document is None:
        return None
    if document.state == "deleted":
        raise DocumentDeleted(f"{url} was deleted; it cannot be re-processed")
    document.content_fingerprint = fingerprint
    document.last_crawled_at = _utcnow()
    document.unit_count = unit_count
    _transition(document, state="processed", vectors_live=document.vectors_live)
    await session.flush()
    return document


async def mark_failed(
    session: AsyncSession,
    *,
    source_id: uuid.UUID,
    url: str,
    code: str,
    detail: dict[str, Any] | None = None,
) -> Document:
    """Record a page-level failure without touching the live vectors.

    A page the crawler *attempted* is registered even if the fetch never
    returned: an operator looking at a source's document list needs to see
    "this page was tried and failed", not an absence they cannot explain. That
    row is created in `discovered` and immediately transitioned, so it never
    appears as a page nobody looked at.

    `vectors_live` is deliberately left as it was. A re-crawl that fails keeps
    the previous version retrievable (FR-054); a first crawl that fails never
    had vectors, and that distinction is the entire reason the column exists
    (data-model §3.2).
    """
    document = await get_document_for_update(session, source_id, url)
    if document is None:
        document = await register_document(session, source_id=source_id, url=url)
    if document.state == "deleted":
        return document
    document.state_detail = {"code": code, **(detail or {})}
    _transition(document, state="failed", vectors_live=document.vectors_live)
    await session.flush()
    return document


async def _units_of(session: AsyncSession, document_id: uuid.UUID) -> list[DocumentUnit]:
    result = await session.execute(
        select(DocumentUnit).where(DocumentUnit.document_id == document_id)
    )
    return list(result.scalars())


async def _drop_units(session: AsyncSession, vector_ids: list[str]) -> None:
    await session.execute(sa_delete(DocumentUnit).where(DocumentUnit.vector_id.in_(vector_ids)))
    await session.flush()


async def _sync_units(
    session: AsyncSession,
    *,
    document_id: uuid.UUID,
    chunks: list[Chunk],
    plan: SupersessionPlan,
) -> None:
    """Make the unit rows match the page, reusing unchanged rows in place.

    A changed ordinal updates its existing row rather than inserting a second
    one: `(document_id, ordinal)` is unique, and a delete-then-insert would
    briefly leave the live vector with no registry row to join against —
    which is the orphan an answer can no longer cite.
    """
    if not chunks:
        return
    write_ordinals = {chunk.ordinal for chunk in plan.to_write}
    existing = {unit.ordinal: unit for unit in await _units_of(session, document_id)}
    previous: Chunk | None = None
    for chunk in chunks:
        repeated = overlap_tokens(chunk, previous)
        previous = chunk
        if chunk.ordinal not in write_ordinals:
            continue
        unit = existing.get(chunk.ordinal)
        if unit is None:
            unit = DocumentUnit(document_id=document_id, ordinal=chunk.ordinal)
            session.add(unit)
        unit.vector_id = vector_id_for(document_id, chunk.ordinal)
        unit.text_fingerprint = unit_text_fingerprint(chunk.text)
        unit.heading_path = list(chunk.heading_path)
        unit.block_types = list(chunk.block_types)
        unit.token_count = chunk.estimated_tokens
        # Measured, not assumed: the column exists so the deliberate overlap of
        # FR-016 can be audited, and a unit that repeats nothing reports zero.
        unit.overlap_tokens = repeated
    await session.flush()


# ============================================================================
# Stage 11 — deletion (T062)
# ============================================================================


@dataclass(frozen=True, slots=True)
class DeletionResult:
    document_id: uuid.UUID
    units_removed: int
    vectors_removed: int
    already_deleted: bool


async def delete_document(
    session: AsyncSession,
    store: VectorStore,
    *,
    document_id: uuid.UUID,
    vector_ids: list[str] | None = None,
) -> DeletionResult:
    """Remove a document's vectors, units, and content, and tombstone it.

    Deletion is the **only** transition that removes vectors (data-model §3.2).
    The vector ids come from the registry's own unit rows by default: the
    registry is the authority on what was indexed, and enumerating it is
    bounded by the document's own unit count. A caller that already holds the
    ids (the list view does) passes them to avoid the read.

    Idempotent: a second call reports `already_deleted` and removes nothing,
    because "delete twice" must not read as "deleted twice".
    """
    document = await session.get(Document, document_id)
    if document is None:
        raise StateConflict(f"document {document_id} does not exist")

    if document.state == "deleted":
        return DeletionResult(
            document_id=document_id, units_removed=0, vectors_removed=0, already_deleted=True
        )

    ids = vector_ids
    if ids is None:
        ids = [unit.vector_id for unit in await _units_of(session, document_id)]

    existing_units = await _units_of(session, document_id)
    vectors_removed = await store.delete(ids) if ids else 0
    await session.execute(sa_delete(DocumentUnit).where(DocumentUnit.document_id == document_id))
    units_removed = len(existing_units)

    # Terminal, with the tombstone set in the same transaction as the removal.
    # A crawl that was mid-flight will fail its next write on this row.
    document.unit_count = 0
    document.state_detail = None
    document.tombstoned_at = _utcnow()
    _transition(document, state="deleted", vectors_live=False)
    await session.flush()

    _log.info(
        "document deleted",
        extra={
            "document_id": str(document_id),
            "units_removed": units_removed,
            "vectors_removed": vectors_removed,
        },
    )
    return DeletionResult(
        document_id=document_id,
        units_removed=units_removed,
        vectors_removed=vectors_removed,
        already_deleted=False,
    )


# ============================================================================
# The crawl orchestrator (T061)
# ============================================================================


@dataclass(slots=True)
class CrawlCounts:
    """Mutable-by-copy counters, one per thing a crawl can do to a page.

    Kept as a dataclass rather than a dict of ints so a typo is a type error,
    and so `as_dict` produces the `skipped_reasons` shape the column declares.
    """

    discovered: int = 0
    processed: int = 0
    unchanged: int = 0
    skipped: int = 0
    failed: int = 0
    indexed: int = 0
    rejected: int = 0
    vectors_written: int = 0
    vectors_deleted: int = 0
    skip_reasons: dict[str, int] = field(default_factory=dict)

    def skip(self, reason: str, count: int = 1) -> None:
        self.skipped += count
        self.skip_reasons[reason] = self.skip_reasons.get(reason, 0) + count

    def as_dict(self) -> dict[str, Any]:
        """Every tally, for logs and debugging. Not an API shape."""
        return {
            "discovered": self.discovered,
            "processed": self.processed,
            "unchanged": self.unchanged,
            "skipped": self.skipped,
            "failed": self.failed,
            "indexed": self.indexed,
            "rejected": self.rejected,
            "vectors_written": self.vectors_written,
            "vectors_deleted": self.vectors_deleted,
            "skip_reasons": dict(sorted(self.skip_reasons.items())),
        }

    def contract_counters(self) -> dict[str, int]:
        """The five counters `CrawlJob.counters` declares, and only those.

        The contract closes that object with `additionalProperties: false`, and
        the tallies it does not name are not lost — `indexed` is `processed`
        minus `unchanged`, and the vectors written are visible on the
        documents. A closed shape is worth the arithmetic.
        """
        return {
            "discovered": self.discovered,
            "processed": self.processed,
            "unchanged": self.unchanged,
            "skipped": self.skipped,
            "failed": self.failed,
        }

    def contract_skipped_reasons(self) -> dict[str, int]:
        """Why pages were not processed, as the flat map the column declares.

        Flat, and integer-valued, because that is what `skipped_reasons` is in
        data-model.md §2.4 and in the contract: `{robots_disallowed: 3,
        too_small: 1}`. Nesting a richer object inside would be a shape no
        client can read with a simple lookup.
        """
        return dict(sorted(self.skip_reasons.items()))


@dataclass(frozen=True, slots=True)
class CrawlResult:
    job_id: uuid.UUID
    status: str
    counts: CrawlCounts
    finished_at: datetime


async def active_job_for(session: AsyncSession, source_id: uuid.UUID) -> CrawlJob | None:
    """The queued/running job for a source, if any (FR-055).

    This is the *readable* half of the guarantee. The binding half is the
    partial unique index `crawl_jobs_one_active_per_source`, which makes a
    second concurrent crawl impossible even when two processes both pass this
    check. The read exists so the loser gets a named job in its error message
    instead of an IntegrityError.
    """
    from app.db.models import ACTIVE_CRAWL_STATUSES

    result = await session.execute(
        select(CrawlJob)
        .where(CrawlJob.source_id == source_id, CrawlJob.status.in_(ACTIVE_CRAWL_STATUSES))
        .order_by(CrawlJob.requested_at.desc())
    )
    return result.scalars().first()


async def start_crawl_job(
    session: AsyncSession,
    *,
    source_id: uuid.UUID,
    target_url: str,
    scope_prefix: str | None = None,
) -> CrawlJob:
    """Claim the source for this crawl, or name the job that already holds it."""
    running = await active_job_for(session, source_id)
    if running is not None:
        raise CrawlAlreadyRunning(job_id=str(running.id))
    job = CrawlJob(
        source_id=source_id,
        target_url=target_url,
        scope_prefix=scope_prefix,
        status="queued",
    )
    session.add(job)
    try:
        await session.flush()
    except Exception as exc:  # noqa: BLE001 - the partial unique index fired
        await session.rollback()
        running = await active_job_for(session, source_id)
        if running is not None:
            raise CrawlAlreadyRunning(job_id=str(running.id)) from exc
        raise
    job.status = "running"
    job.started_at = _utcnow()
    await session.flush()
    return job


async def finish_crawl_job(
    session: AsyncSession,
    *,
    job: CrawlJob,
    counts: CrawlCounts,
    status: str | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
) -> CrawlJob:
    """Close a job with its counts. The status is derived, never guessed."""
    if status is None:
        status = "completed" if counts.failed == 0 else "completed_with_errors"
    job.status = status
    job.pages_discovered = counts.discovered
    job.pages_processed = counts.processed
    job.pages_unchanged = counts.unchanged
    job.pages_skipped = counts.skipped
    job.pages_failed = counts.failed
    job.skipped_reasons = counts.contract_skipped_reasons()
    job.error_code = error_code
    job.error_message = error_message
    job.finished_at = _utcnow()
    await session.flush()
    return job


async def run_crawl(
    session: AsyncSession,
    store: VectorStore,
    *,
    source_id: uuid.UUID,
    fetch: Callable[[str], Any],
    candidates: list[tuple[str, int, str]],
    max_pages: int | None = None,
    max_depth: int | None = None,
    delay_seconds: float | None = None,
    sleep: Callable[[float], Any] = asyncio.sleep,
    scope_prefix: str | None = None,
    progress: Callable[[str], None] | None = None,
    job: CrawlJob | None = None,
) -> CrawlResult:
    """Crawl a bounded set of pages, recording every outcome.

    The bounds are enforced here rather than by the caller, because a bound
    that a caller must remember is a bound that is one forgotten argument away
    from not existing. `max_pages`, `max_depth`, and `delay_seconds` default
    to the configured crawler settings and can only be lowered by the caller,
    which is what keeps a test honest: a test that passes `max_pages=1` is
    testing a real bound, not a mocked one.

    **A failed page does not abort the run.** Every per-page failure is caught,
    counted, and recorded against the job, then the next page is fetched. A
    crawl that stops at the first 500 loses the whole corpus behind one bad
    URL (FR-054).
    """
    settings = get_settings()
    page_cap = (
        settings.crawl_max_pages if max_pages is None else min(max_pages, settings.crawl_max_pages)
    )
    depth_cap = (
        settings.crawl_max_depth if max_depth is None else min(max_depth, settings.crawl_max_depth)
    )
    delay = settings.crawl_delay_seconds if delay_seconds is None else max(0.0, delay_seconds)

    counts = CrawlCounts()
    source = await session.get(Source, source_id)
    if source is None:
        raise StateConflict(f"source {source_id} does not exist")

    if job is None:
        job = await start_crawl_job(
            session,
            source_id=source_id,
            target_url=source.start_url,
            scope_prefix=scope_prefix,
        )
        # Committed before the first fetch. The job row is the audit trail, and
        # it holds the partial unique index that makes FR-055 true — leaving it
        # uncommitted for the length of the run would mean a crash erased the
        # very record that explains the crash, and released the source while the
        # document list still claimed it was being crawled.
        await session.commit()

    try:
        selected = [c for c in candidates if c[1] <= depth_cap][:page_cap]
        counts.discovered = len(selected)
        job.pages_discovered = len(selected)

        for position, (url, depth, via) in enumerate(selected):
            if position:
                if delay:
                    await sleep(delay)

            # FR-056, before anything is fetched or written: a tombstoned
            # document is never resurrected, and its vectors were already gone.
            document = await get_document(session, source_id, url)
            if document is not None and document.tombstoned_at is not None:
                counts.skip("tombstoned")
                continue

            # One transaction per page. A rollback undoes the failed page and
            # nothing else — not the job row, not the pages already indexed
            # this run, and not the previous version of this page. A crawl that
            # shares a single transaction for the whole run cannot honour
            # FR-054 at all: the first failure would erase the run's work
            # along with itself.
            try:
                response = await fetch(url)
                content_type = response.headers.get("content-type")
                ingestion = ingest_page(response.text, url=url, content_type=content_type)
            except RobotsDisallowed:
                # A refusal by the site's own directives is not a failure: the
                # page was never fetched, nothing was attempted, and the run is
                # correct. Recorded as a skip with its own reason so
                # `pages_skipped` and `skipped_reasons` answer "why did the
                # crawler not read that page" without a client inferring it
                # from an error code (FR-028, FR-054).
                counts.skip("robots_disallowed")
                _log.info("page refused by robots", extra={"url": url, "via": via})
                continue
            except IngestionRejected as exc:
                counts.rejected += 1
                counts.skip(exc.reason)
                _log.info("page rejected", extra={"url": url, "reason": exc.reason, "via": via})
                continue
            except Exception as exc:  # noqa: BLE001 - one page must not end the crawl
                await session.rollback()
                counts.failed += 1
                await _record_page_failure(session, source_id, url, exc)
                _log.warning(
                    "page failed",
                    extra={"url": url, "error": type(exc).__name__, "depth": depth, "via": via},
                )
                continue

            try:
                outcome = await index_page(
                    session,
                    store,
                    source_id=source_id,
                    ingestion=ingestion,
                )
                await session.commit()
            except DocumentDeleted:
                # Deletion won the race (FR-056). The write path refused, so
                # the page's transaction is discarded rather than committed.
                await session.rollback()
                counts.skip("tombstoned")
                continue
            except Exception as exc:  # noqa: BLE001 - see above
                await session.rollback()
                counts.failed += 1
                await _record_page_failure(session, source_id, url, exc)
                _log.warning(
                    "indexing failed",
                    extra={"url": url, "error": type(exc).__name__, "depth": depth},
                )
                continue

            counts.processed += 1
            if outcome.unchanged:
                counts.unchanged += 1
            else:
                counts.indexed += 1
            counts.vectors_written += outcome.written
            counts.vectors_deleted += outcome.deleted
            if progress is not None:
                progress(url)

        await session.commit()
    except Exception as exc:  # noqa: BLE001 - the run itself failed, not one page
        await session.rollback()
        job = await fail_crawl_job(session, job_id=job.id, exc=exc)
        raise

    await finish_crawl_job(session, job=job, counts=counts)
    await session.commit()
    return CrawlResult(
        job_id=job.id,
        status=job.status,
        counts=counts,
        finished_at=job.finished_at or _utcnow(),
    )


@dataclass(frozen=True, slots=True)
class PageOutcome:
    """What one page did, in the vocabulary the crawl counters speak.

    `unchanged` is a field rather than a counter because the two outcomes are
    not the same magnitude: an unchanged page wrote nothing at all, and a page
    that was indexed wrote `written` vectors and may have removed `deleted`.
    """

    url: str
    unchanged: bool
    written: int
    deleted: int
    units: int


async def index_page(
    session: AsyncSession,
    store: VectorStore,
    *,
    source_id: uuid.UUID,
    ingestion: PageIngestion,
) -> PageOutcome:
    """Fingerprint gate, then persistence, for one already-ingested page.

    Unchanged content is the common case on a re-crawl and must not re-embed
    a single token (SC-012, FR-026). The gate compares against the *stored*
    fingerprint, which is only correct because §3.3 writes the fingerprint in
    the same transaction as the vectors it describes.

    `processed` is written in both branches on purpose. Extraction and
    chunking did happen, and a page that was read but not re-indexed is a
    different fact from a page that was never read (FR-053).
    """
    document = await get_document_for_update(session, source_id, ingestion.url)
    if document is not None and document.state == "deleted":
        raise DocumentDeleted(f"{ingestion.url} was deleted mid-crawl")

    prior_fingerprint = document.content_fingerprint if document is not None else None
    prior_units = document.unit_count if document is not None else 0
    decision = classify_change(prior_fingerprint, ingestion.content_fingerprint)

    await mark_processed(
        session,
        source_id=source_id,
        url=ingestion.url,
        fingerprint=ingestion.content_fingerprint,
        unit_count=prior_units if decision == UNCHANGED else len(ingestion.chunks.chunks),
    )

    if decision == UNCHANGED:
        return PageOutcome(
            url=ingestion.url,
            unchanged=True,
            written=0,
            deleted=0,
            units=prior_units,
        )

    persisted = await persist_indexed_document(
        session,
        store,
        source_id=source_id,
        ingestion=ingestion,
        result=ingestion.chunks,
    )
    return PageOutcome(
        url=ingestion.url,
        unchanged=False,
        written=persisted.written,
        deleted=persisted.deleted,
        units=len(ingestion.chunks.chunks),
    )


async def _record_page_failure(
    session: AsyncSession, source_id: uuid.UUID, url: str, exc: BaseException
) -> None:
    """Persist a per-page failure, then roll it back so the run continues.

    The rollback is the point: a failed page must leave no half-written rows,
    and the transaction it was attempted in is the one that has to be undone.
    """
    await session.rollback()
    code = _error_code(exc)
    try:
        await mark_failed(
            session,
            source_id=source_id,
            url=url,
            code=code,
            detail={"message": _safe_message(exc)},
        )
        await session.commit()
    except Exception:  # noqa: BLE001 - recording a failure must not end the crawl
        await session.rollback()
        _log.error("could not record page failure", extra={"url": url, "error": type(exc).__name__})


async def fail_crawl_job(
    session: AsyncSession, *, job_id: uuid.UUID, exc: BaseException
) -> CrawlJob:
    """Close a job as `failed` and commit it, whatever state the run left behind.

    Public because a caller can fail *before* `run_crawl` starts — the start page
    fetch and the sitemap read happen in `crawl_service` — and a crawl whose
    failure precedes the orchestrator needs the same closing row written by the
    same code. The rollback is unconditional: this is the failure path, and
    whatever the failed step left uncommitted is not something to publish.
    """
    await session.rollback()
    job = await session.get(CrawlJob, job_id)
    if job is not None:
        job.status = "failed"
        job.error_code = _error_code(exc)
        job.error_message = _safe_message(exc)
        job.finished_at = _utcnow()
        await session.commit()
    return job  # type: ignore[return-value]


def _error_code(exc: BaseException) -> str:
    """The stable code for a failure, never the exception class name alone.

    A job's `error_code` is something an operator can alert on. "ValueError"
    is not; the code vocabulary in `errors.py` is, and it is closed — so a
    network timeout maps to `INTERNAL_ERROR` rather than to a code invented on
    the spot. The specific cause travels in the safe message instead, where it
    is still readable and still bounded.
    """
    from app.core.errors import AppError, ErrorCode

    if isinstance(exc, AppError):
        return str(exc.code)
    return str(ErrorCode.INTERNAL_ERROR)


def _safe_message(exc: BaseException) -> str:
    """A message safe to store and render.

    Bounded and, where the exception carries one, redacted by the exception's
    own constructor (FR-047: no stack trace, no provider error body, no
    credential). An unexpected exception contributes only its type, because
    its message is by definition not something anyone vetted for this column.
    """
    from app.core.errors import AppError

    if isinstance(exc, AppError):
        return exc.message[:500]
    return f"{type(exc).__name__} during crawl"


async def iter_source_documents(
    session: AsyncSession, source_id: uuid.UUID, *, include_deleted: bool = False
) -> AsyncIterator[Document]:
    """Documents of a source, oldest first, for the list views."""
    stmt = select(Document).where(Document.source_id == source_id)
    if not include_deleted:
        stmt = stmt.where(Document.state != "deleted")
    result = await session.execute(stmt.order_by(Document.created_at.asc()))
    for document in result.scalars():
        yield document


__all__ = [
    "CrawlCounts",
    "CrawlResult",
    "DeletionResult",
    "PageOutcome",
    "PersistResult",
    "SupersessionPlan",
    "active_job_for",
    "delete_document",
    "fail_crawl_job",
    "finish_crawl_job",
    "get_document",
    "get_document_for_update",
    "index_page",
    "index_units",
    "ingest_page",
    "iter_source_documents",
    "mark_failed",
    "mark_processed",
    "persist_indexed_document",
    "plan_supersession",
    "register_document",
    "run_crawl",
    "start_crawl_job",
    "unit_metadata",
    "vector_id_for",
]
