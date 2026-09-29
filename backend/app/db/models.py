"""The nine tables of the relational registry.

data-model.md §2, in full. The registry is authoritative for provenance: the
vector store holds vectors and a denormalised copy of the query-path metadata,
but every question about *what exists* is answered from here.

Two things are worth stating before the column definitions, because both look
like omissions and are not.

**Unit text is not stored.** `document_units` records identity, ordering,
heading path, and block types — not the text. The text lives in the vector store
as the embedded field. Duplicating it would create two copies of third-party
content to keep in sync, and would put scraped documentation into the
relational store for no retrieval benefit (FR-032 is about version control, but
the same reasoning holds with more force for a database nobody has audited).

**Two counts are deliberately not columns.** `sources.page_count` and
`sources.chunk_count` appear in the spec's Source entity but are not stored —
they are computed on read from `documents` and `document_units`. Persisting them
invites drift against the tables that are the authority, and a registry that can
disagree with itself is worse than a registry that has to do a COUNT.

The one exception to "no page content" is `citations.quote`, which holds a
bounded supporting span so a reviewer can check that a cited span supports its
claim without re-fetching the page (data-model.md §2.6).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# ============================================================================
# Base
# ============================================================================


class Base(DeclarativeBase):
    """Declarative base for the registry.

    `metadata` is the single source Alembic autogenerate diffs against, so a
    model change that does not appear in a migration is a change the migration
    will silently revert.
    """


#: `TIMESTAMPTZ` everywhere. `created_at` on a UTC-only system and
#: `created_at` on a system in another zone must not produce different values for
#: the same instant, and every comparison in the retention purge would be wrong
#: for half the fleet otherwise. Python side is always timezone-aware UTC.
def utcnow() -> datetime:
    from datetime import UTC

    return datetime.now(UTC)


def uuid_pk() -> Mapped[uuid.UUID]:
    """A generated UUIDv4 primary key.

    Generated in Python rather than by the database so a test can construct a
    valid object graph without a round trip, and so the ID exists before the
    insert — which the ingestion pipeline needs, because `vector_id` embeds
    `document_id` and vectors are written before the transaction commits.
    """
    return mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


def in_clause(column: str, permitted: tuple[str, ...]) -> str:
    """A `CHECK` constraint expression restricting a column to a fixed set.

    Generated from the same tuple the Python code validates against, so the
    permitted values are a contract rather than a copy. data-model.md §2 notes
    the choice of `CHECK`-constrained `TEXT` over a native enum is a migration
    concern; the permitted VALUES are a contract, and this keeps one list.
    """
    values = ", ".join(repr(value) for value in permitted)
    return f"{column} IN ({values})"


# ============================================================================
# 2.1 sources
# ============================================================================


class Source(Base):
    """A registered public documentation entry point.

    `start_url` is unique, which is what makes `POST /sources` idempotent:
    re-registering an existing source returns the existing resource with `200`
    rather than `409` (contracts/README.md, Idempotency and concurrency).
    """

    __tablename__ = "sources"

    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(Text, nullable=False)
    start_url: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    # Derived from the registrant's entry through the address guard, never from
    # a client-supplied string that bypassed the guard (data-model.md §2.1).
    allowed_domains: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False)
    # null = a cross-product source. FR-009 forbids forcing a classification, so
    # this column is nullable for a reason and the null is meaningful.
    product_domain: Mapped[str | None] = mapped_column(String, nullable=True)
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    status: Mapped[str] = mapped_column(
        String, nullable=False, default="active", server_default="active"
    )
    last_crawl_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    manifest_version: Mapped[str | None] = mapped_column(String, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        onupdate=utcnow,
        server_default=func.now(),
    )

    documents: Mapped[list[Document]] = relationship(
        back_populates="source", cascade="all, delete-orphan", passive_deletes=True
    )
    crawl_jobs: Mapped[list[CrawlJob]] = relationship(
        back_populates="source", cascade="all, delete-orphan", passive_deletes=True
    )

    __table_args__ = (
        CheckConstraint("name <> ''", name="ck_sources_name_non_empty"),
        CheckConstraint("start_url <> ''", name="ck_sources_start_url_non_empty"),
        CheckConstraint("status IN ('active', 'disabled', 'error')", name="ck_sources_status"),
        CheckConstraint(
            "cardinality(allowed_domains) > 0", name="ck_sources_allowed_domains_non_empty"
        ),
        # FR-009: a null product is an explicit unknown, not a missing value.
        CheckConstraint(
            "product_domain IS NULL OR product_domain IN ('jira', 'confluence', 'jsm', 'developer')",
            name="ck_sources_product_domain",
        ),
    )


# ============================================================================
# 2.2 documents
# ============================================================================

#: The six states of data-model.md §3.1.
DOCUMENT_STATES: tuple[str, ...] = (
    "discovered",
    "crawling",
    "processed",
    "indexed",
    "failed",
    "deleted",
)


class Document(Base):
    """One ingested page, carrying the lifecycle (FR-053) and provenance (FR-024)."""

    __tablename__ = "documents"

    id: Mapped[uuid.UUID] = uuid_pk()
    source_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("sources.id", ondelete="CASCADE"), nullable=False
    )
    url: Mapped[str] = mapped_column(Text, nullable=False)
    # Used for deduplication when a publisher serves the same page at two URLs.
    # Not unique: two entries may share a canonical URL, and the crawl records
    # the decision on the job rather than blocking the second document.
    canonical_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    product: Mapped[str | None] = mapped_column(Text, nullable=True)
    category: Mapped[str | None] = mapped_column(Text, nullable=True)
    page_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    language: Mapped[str | None] = mapped_column(Text, nullable=True)
    # SHA-256 of normalised main content. Drives FR-026 unchanged-detection.
    content_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # "version information where detectable" (FR-024). Null when the publisher
    # exposes no usable Last-Modified or ETag, which is common.
    source_modified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_crawled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Mirrors document_units for list views. Denormalised deliberately, and
    # written in the same transaction as the units themselves so the two cannot
    # disagree for longer than a transaction.
    unit_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    state: Mapped[str] = mapped_column(
        String, nullable=False, default="discovered", server_default="discovered"
    )
    # Error code and safe message when `failed`. Never a stack trace (FR-047).
    state_detail: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    # data-model.md §3.2: vectors are removed ONLY on transition to `deleted`.
    # This flag is not derivable from `state` alone, because `failed` means
    # opposite things depending on the prior state — a re-crawl that failed
    # keeps its old content live, a first crawl that failed has nothing to keep.
    vectors_live: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    indexed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    tombstoned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        onupdate=utcnow,
        server_default=func.now(),
    )

    source: Mapped[Source] = relationship(back_populates="documents")
    units: Mapped[list[DocumentUnit]] = relationship(
        back_populates="document",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="DocumentUnit.ordinal",
    )

    __table_args__ = (
        UniqueConstraint("source_id", "url", name="uq_documents_source_url"),
        CheckConstraint(in_clause("state", DOCUMENT_STATES), name="ck_documents_state"),
        # A tombstone without the terminal state would leave content queryable,
        # which is exactly what FR-056 forbids. Enforced in the database so it
        # cannot be violated by a code path that forgets the check.
        CheckConstraint(
            "tombstoned_at IS NULL OR state = 'deleted'",
            name="ck_documents_tombstone_implies_deleted",
        ),
        # §3.2: deletion is the only transition that removes vectors, so
        # `vectors_live` may never be true on a deleted document.
        CheckConstraint(
            "state <> 'deleted' OR vectors_live = false",
            name="ck_documents_deleted_has_no_vectors",
        ),
        Index("ix_documents_state", "state"),
        Index("ix_documents_source_live", "source_id", postgresql_where=(state != "deleted")),
        Index(
            "ix_documents_tombstoned",
            "tombstoned_at",
            postgresql_where=(tombstoned_at.isnot(None)),
        ),
    )


# ============================================================================
# 2.3 document_units
# ============================================================================


class DocumentUnit(Base):
    """A retrievable, structure-derived segment (FR-015, FR-018).

    No `text` column, by design — see the module docstring. Provenance and
    identity only; the text is the embedded field in the vector store.
    """

    __tablename__ = "document_units"

    id: Mapped[uuid.UUID] = uuid_pk()
    document_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    # R-005: `{document_id}#{ordinal:04d}`. IMMUTABLE — regenerating it would
    # orphan live vectors, which is the one failure that is invisible until
    # something is deleted and something else is not.
    vector_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    text_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    # May be empty, but NOT null. An empty path means the unit is above the
    # first heading; a null would be indistinguishable from "extraction lost
    # the structure", and FR-018 needs to tell those apart.
    heading_path: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False, default=list)
    block_types: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False, default=list)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False)
    # Deliberate overlap with the previous unit (FR-016). Stored so the overlap
    # can be audited and reported rather than assumed.
    overlap_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now()
    )

    document: Mapped[Document] = relationship(back_populates="units")

    __table_args__ = (
        UniqueConstraint("document_id", "ordinal", name="uq_document_units_document_ordinal"),
        CheckConstraint("ordinal >= 0", name="ck_document_units_ordinal_non_negative"),
        CheckConstraint("token_count > 0", name="ck_document_units_token_count_positive"),
        CheckConstraint("overlap_tokens >= 0", name="ck_document_units_overlap_non_negative"),
    )


# ============================================================================
# 2.4 crawl_jobs
# ============================================================================

CRAWL_JOB_STATUSES: tuple[str, ...] = (
    "queued",
    "running",
    "completed",
    "completed_with_errors",
    "failed",
    "cancelled",
)

#: The statuses that count as "active" for the FR-055 partial unique index.
ACTIVE_CRAWL_STATUSES: tuple[str, ...] = ("queued", "running")


class CrawlJob(Base):
    """One crawl run against a source (FR-025, FR-028, FR-054).

    `pages_unchanged` and `pages_skipped` are columns rather than derivations
    because they are the evidence for two success criteria: SC-012 needs a count
    of pages correctly identified as unchanged, and a skip is a decision that
    has to be attributable afterwards.
    """

    __tablename__ = "crawl_jobs"

    id: Mapped[uuid.UUID] = uuid_pk()
    source_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("sources.id", ondelete="CASCADE"), nullable=False
    )
    target_url: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        String, nullable=False, default="queued", server_default="queued"
    )
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    pages_discovered: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    pages_processed: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    pages_unchanged: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    pages_skipped: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    pages_failed: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    # `{robots_disallowed: 3, too_boilerplate: 1, duplicate: 2}` — which pages
    # were NOT processed, and why, in a form an operator can read.
    skipped_reasons: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String, nullable=True)
    # Safe, human-readable. Never a stack trace, a provider error body, or a
    # credential (FR-047).
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The effective scope prefix the crawl was confined to, recorded so "why
    # did the crawler visit this page" is answerable from the audit trail.
    scope_prefix: Mapped[str | None] = mapped_column(Text, nullable=True)

    source: Mapped[Source] = relationship(back_populates="crawl_jobs")

    __table_args__ = (
        CheckConstraint(in_clause("status", CRAWL_JOB_STATUSES), name="ck_crawl_jobs_status"),
        # FR-055 as a DATABASE guarantee, not application logic. A partial unique
        # index cannot be satisfied by two rows, so it holds under concurrency
        # and cannot be bypassed by a code path that forgets to check.
        Index(
            "crawl_jobs_one_active_per_source",
            "source_id",
            unique=True,
            postgresql_where=(status.in_(ACTIVE_CRAWL_STATUSES)),
        ),
        Index("ix_crawl_jobs_source_requested", "source_id", "requested_at"),
    )


# ============================================================================
# 2.5 query_logs
# ============================================================================


class QueryLog(Base):
    """Audit record of one answered question (FR-048), host of the inspection trace.

    `id` IS the `request_id` returned to the client. That is deliberate: the
    value a user quotes in a bug report is the same value that joins the log to
    the audit record, so there is nothing to translate and no chance of the two
    drifting (FR-047).
    """

    __tablename__ = "query_logs"

    id: Mapped[uuid.UUID] = uuid_pk()
    question: Mapped[str] = mapped_column(Text, nullable=False)
    outcome: Mapped[str] = mapped_column(String, nullable=False)
    # null on refusal and on error. A refusal has no answer; storing an empty
    # string instead would be indistinguishable from an answer that was empty.
    answer: Mapped[str | None] = mapped_column(Text, nullable=True)
    refusal_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # null = an explicit unknown, not a missing value (FR-009).
    detected_product: Mapped[str | None] = mapped_column(String, nullable=True)
    detected_category: Mapped[str | None] = mapped_column(String, nullable=True)
    intent: Mapped[str | None] = mapped_column(String, nullable=True)
    classification_confidence: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    rewrite_queries: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False, default=list)
    applied_filters: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    # FR-048 requires these counts explicitly, which is why they are columns
    # rather than something a reader has to infer from the trace.
    candidates_retrieved: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    candidates_reranked: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    evidence_selected: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    stage_latency_ms: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    total_latency_ms: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    provider: Mapped[str | None] = mapped_column(String, nullable=True)
    model: Mapped[str | None] = mapped_column(String, nullable=True)
    # prompt_tokens, completion_tokens, total_tokens, total_time (R-008).
    token_usage: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    rerank_model: Mapped[str | None] = mapped_column(String, nullable=True)
    # Populated ONLY when inspection mode is on (FR-033). The four pipeline
    # artefacts are request-scoped value objects; snapshotting them here is
    # what makes the inspection view work after the request has returned.
    pipeline_trace: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String, nullable=True)
    # NO secret or provider-credential column exists, by design (FR-042).
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now()
    )

    citations: Mapped[list[Citation]] = relationship(
        back_populates="query_log",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="Citation.rank",
    )

    __table_args__ = (
        CheckConstraint(
            "outcome IN ('answered', 'refused', 'error')", name="ck_query_logs_outcome"
        ),
        # An answer and a refusal cannot both be present: `answer` is non-null
        # exactly when `outcome = 'answered'`.
        CheckConstraint(
            "(outcome = 'answered' AND answer IS NOT NULL AND refusal_reason IS NULL) OR "
            "(outcome = 'refused' AND answer IS NULL AND refusal_reason IS NOT NULL) OR "
            "(outcome = 'error' AND answer IS NULL)",
            name="ck_query_logs_outcome_payload",
        ),
        CheckConstraint("total_latency_ms >= 0", name="ck_query_logs_latency_non_negative"),
        # Indexed for the bounded-retention purge (FR-048). Without this a
        # 30-day purge is a sequential scan of every question ever asked.
        Index("ix_query_logs_created_at", "created_at"),
    )


# ============================================================================
# 2.6 citations
# ============================================================================


class Citation(Base):
    """A validated evidence reference attached to an answer (FR-002, FR-004).

    `source_url` is LOOKED UP from `documents`, never taken from model output.
    FR-003 forbids the model authoring a source link and FR-004 requires
    validation to confirm the link matches the registry of record, so the link
    the model claimed is compared during validation and the outcome recorded in
    `validation_state` / `validation_note` — it is not stored as if it were true.
    """

    __tablename__ = "citations"

    id: Mapped[uuid.UUID] = uuid_pk()
    query_log_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("query_logs.id", ondelete="CASCADE"), nullable=False
    )
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    document_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("documents.id"), nullable=False
    )
    unit_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("document_units.id"), nullable=False
    )
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    # Denormalised for citation display: rendering a citation should not need
    # a second query per row to show a title and a product.
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    product: Mapped[str | None] = mapped_column(String, nullable=True)
    category: Mapped[str | None] = mapped_column(String, nullable=True)
    heading_path: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False, default=list)
    # The ONLY place third-party text is retained outside the vector store. Bounded
    # to the supporting span, stored in the database rather than version control,
    # and present so a reviewer can check the claim without re-fetching the page.
    quote: Mapped[str] = mapped_column(Text, nullable=False)
    # Retrieval and rerank scores are stored SEPARATELY and never averaged or
    # normalised onto a shared scale: scores from different models are not
    # comparable (contracts/README.md, "Money, scores, and thresholds").
    retrieval_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    rerank_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    validation_state: Mapped[str] = mapped_column(
        String, nullable=False, default="valid", server_default="valid"
    )
    validation_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    query_log: Mapped[QueryLog] = relationship(back_populates="citations")

    __table_args__ = (
        UniqueConstraint("query_log_id", "rank", name="uq_citations_query_log_rank"),
        CheckConstraint("rank > 0", name="ck_citations_rank_positive"),
        CheckConstraint(
            "validation_state IN ('valid', 'stripped', 'repaired')",
            name="ck_citations_validation_state",
        ),
        CheckConstraint("quote <> ''", name="ck_citations_quote_non_empty"),
        CheckConstraint(
            "retrieval_score IS NULL OR (retrieval_score >= 0.0 AND retrieval_score <= 1.0)",
            name="ck_citations_retrieval_score_range",
        ),
        CheckConstraint(
            "rerank_score IS NULL OR (rerank_score >= 0.0 AND rerank_score <= 1.0)",
            name="ck_citations_rerank_score_range",
        ),
        Index("ix_citations_query_log_rank", "query_log_id", "rank"),
    )


# ============================================================================
# 2.7 evaluation_questions
# ============================================================================

EVALUATION_CATEGORIES: tuple[str, ...] = (
    "simple_lookup",
    "multi_part",
    "cross_product",
    "troubleshooting",
    "permissions",
    "api",
    "ambiguous",
    "unsupported",
)
EVALUATION_DIFFICULTIES: tuple[str, ...] = ("easy", "medium", "hard")


class EvaluationQuestion(Base):
    """A curated assessment item (FR-039, FR-040, FR-063).

    There is deliberately NO `source_url` column. Grading is by topic coverage,
    so a publisher reorganising their documentation cannot invalidate the
    dataset — and the same exclusion is enforced structurally in
    `contracts/evaluation-dataset.schema.json`, so a pinned URL cannot be
    smuggled in through the committed file either. Two independent mechanisms,
    because the file and the database are loaded by different code.
    """

    __tablename__ = "evaluation_questions"

    # TEXT, not UUID: the ID is a stable human-readable slug from the committed
    # dataset, and it is the join key a reviewer reads in docs/EVALUATION.md.
    id: Mapped[str] = mapped_column(String, primary_key=True)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(String, nullable=False)
    difficulty: Mapped[str] = mapped_column(String, nullable=False)
    # The deliberately-unsupported bucket behind SC-008.
    is_unsupported: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    expected_product: Mapped[str | None] = mapped_column(String, nullable=True)
    expected_category: Mapped[str | None] = mapped_column(String, nullable=True)
    expected_heading_path: Mapped[list[str]] = mapped_column(
        ARRAY(String), nullable=False, default=list
    )
    # Each entry is one checkable assertion, not a topic label. That is what
    # makes claim coverage (SC-006) falsifiable rather than a matter of taste.
    expected_topics: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False)
    # FR-040: several answers may be valid, and saying so is part of the item.
    answer_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    tags: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False, default=list)
    dataset_version: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            in_clause("category", EVALUATION_CATEGORIES), name="ck_evaluation_questions_category"
        ),
        CheckConstraint(
            in_clause("difficulty", EVALUATION_DIFFICULTIES),
            name="ck_evaluation_questions_difficulty",
        ),
        CheckConstraint(
            "cardinality(expected_topics) > 0", name="ck_evaluation_questions_topics_non_empty"
        ),
        CheckConstraint("question <> ''", name="ck_evaluation_questions_question_non_empty"),
    )


# ============================================================================
# 2.8 evaluation_runs, evaluation_results
# ============================================================================


class EvaluationRun(Base):
    """One execution of the suite (FR-036, FR-041).

    `thresholds` and `gate_outcome` are what make a run honest. `thresholds`
    records the gate values that were in force, so two runs are never silently
    compared across different gates. `gate_outcome` is strictly `pass` or
    `fail` — never a soft "warning", because an outcome that can render as
    "marginal" lets a failing run read as a soft result (SC-001…SC-009).
    """

    __tablename__ = "evaluation_runs"

    id: Mapped[uuid.UUID] = uuid_pk()
    dataset_version: Mapped[str] = mapped_column(String, nullable=False)
    # Content hash. A run is never comparable across datasets, and without a
    # fingerprint "same dataset version" is an assertion nobody can check.
    dataset_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    # Model, embed model, retrieval budget, thresholds used. The full input to
    # the run, so a surprising number can be attributed to a configuration
    # change rather than to a mystery.
    config_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    question_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    # Every value computed from THIS run (FR-036). No default, no placeholder:
    # a run with no metrics is a run that did not happen.
    metrics: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    # FR-065: the gate values in force. Recorded BEFORE the run is judged and
    # never derived from the run's own results.
    thresholds: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    gate_outcome: Mapped[str] = mapped_column(String, nullable=False)
    # `[]` when passing; otherwise which gate, its target, and its actual value.
    gate_failures: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    corpus_document_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    corpus_unit_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    results: Mapped[list[EvaluationResult]] = relationship(
        back_populates="run", cascade="all, delete-orphan", passive_deletes=True
    )

    __table_args__ = (
        CheckConstraint("gate_outcome IN ('pass', 'fail')", name="ck_evaluation_runs_gate_outcome"),
        CheckConstraint(
            "question_count >= 0", name="ck_evaluation_runs_question_count_non_negative"
        ),
        CheckConstraint(
            "finished_at IS NULL OR finished_at >= started_at", name="ck_evaluation_runs_time_order"
        ),
    )


class EvaluationResult(Base):
    """One row per question per run.

    The two absolute-zero counters — `fabricated_fact_count` and
    `invalid_citation_count` — are COUNTS, not ratios, and deliberately have no
    configuration. FR-066: an absolute zero that can be configured is not a
    zero. Every other metric is nullable and a null means *not applicable to
    this question*, never *not measured*.
    """

    __tablename__ = "evaluation_results"

    id: Mapped[uuid.UUID] = uuid_pk()
    run_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("evaluation_runs.id", ondelete="CASCADE"), nullable=False
    )
    question_id: Mapped[str] = mapped_column(
        String, ForeignKey("evaluation_questions.id"), nullable=False
    )
    # null for an `is_unsupported` question: retrieval grading is undefined
    # where the right answer is a refusal. Recording 0.0 there would drag a real
    # average down and misrepresent a correct refusal as a retrieval failure.
    recall_at_5: Mapped[float | None] = mapped_column(Float, nullable=True)
    precision_at_5: Mapped[float | None] = mapped_column(Float, nullable=True)
    mrr: Mapped[float | None] = mapped_column(Float, nullable=True)
    citation_validity: Mapped[float | None] = mapped_column(Float, nullable=True)
    claim_coverage: Mapped[float | None] = mapped_column(Float, nullable=True)
    faithfulness: Mapped[float | None] = mapped_column(Float, nullable=True)
    refused: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    # MUST be 0 (SC-008). A count, not a ratio: no configuration can move it.
    fabricated_fact_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    # MUST be 0 (SC-009). Same reasoning.
    invalid_citation_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    # Domains actually searched, for the cross-product coverage gate. A list so
    # "searched two products" is inspectable rather than a bare count.
    searched_products: Mapped[list[str]] = mapped_column(
        ARRAY(String), nullable=False, default=list
    )
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    run: Mapped[EvaluationRun] = relationship(back_populates="results")

    __table_args__ = (
        UniqueConstraint("run_id", "question_id", name="uq_evaluation_results_run_question"),
        CheckConstraint(
            "fabricated_fact_count >= 0", name="ck_evaluation_results_fabricated_non_negative"
        ),
        CheckConstraint(
            "invalid_citation_count >= 0",
            name="ck_evaluation_results_invalid_citations_non_negative",
        ),
        CheckConstraint(
            "latency_ms IS NULL OR latency_ms >= 0",
            name="ck_evaluation_results_latency_non_negative",
        ),
    )


__all__ = [
    "ACTIVE_CRAWL_STATUSES",
    "Base",
    "CRAWL_JOB_STATUSES",
    "CrawlJob",
    "DOCUMENT_STATES",
    "Document",
    "DocumentUnit",
    "EVALUATION_CATEGORIES",
    "EVALUATION_DIFFICULTIES",
    "EvaluationQuestion",
    "EvaluationResult",
    "EvaluationRun",
    "QueryLog",
    "Source",
]
