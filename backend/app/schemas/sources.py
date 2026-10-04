"""Request and response schemas for sources, documents, units, and crawl jobs.

`contracts/openapi.yaml` is the authority for every shape here, and the contract
is closed: `additionalProperties: false` on all of them. That is inherited from
`ApiModel` rather than repeated per class, because a schema that forgot it would
still validate against the contract file — the suite validates the *contract*,
not the schema — and the extra field would reach a client undeclared.

Three decisions are worth stating, because each one is a place where a looser
implementation would also pass:

**`DocumentUnitOut` carries no text.** The contract says so, and it is the
load-bearing part: unit text lives in the vector store, and an endpoint that
returned it would turn the corpus view into a content dump — a bulk exfiltration
route behind a read-only permission (FR-031). Provenance only.

**`CrawlJobCounters` has exactly five fields.** `CrawlCounts` carries more
(vectors written, rejected, per-reason skips) for logs. The contract's closure is
honoured by arithmetic rather than by widening: `indexed` is `processed` minus
`unchanged`, and `skipped_reasons` carries the per-reason breakdown as its own
flat map.

**`SourceUpdate.product_domain` can be set to `null`, and "absent" is a third
state.** FR-009 forbids forcing a classification, so a client must be able to say
"I do not know what product this is" for a source. That is only expressible if
the field can be explicitly `null` *and* if leaving it out is a different request
— hence `model_fields_set` and `exclude_unset=True` in the route. A model that
treated absent as `null` would silently erase a classification every time a client
patched a source's name.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import Field, HttpUrl

from app.schemas import ApiModel, Page

# ============================================================================
# Enumerations
# ============================================================================


class SourceStatus(StrEnum):
    """Registry-side health of a source, as the contract's three values.

    Distinct from `enabled`: `enabled` is what the operator asked for, `status`
    is what the registry observed. A source can be enabled and in `error`.
    """

    ACTIVE = "active"
    DISABLED = "disabled"
    ERROR = "error"


class DocumentState(StrEnum):
    """The six-state lifecycle of data-model.md §3.1."""

    DISCOVERED = "discovered"
    CRAWLING = "crawling"
    PROCESSED = "processed"
    INDEXED = "indexed"
    FAILED = "failed"
    DELETED = "deleted"


class CrawlJobStatus(StrEnum):
    """Six terminal and non-terminal states.

    `completed_with_errors` exists because collapsing it into `completed` would
    make a crawl that failed half its pages indistinguishable from a clean one
    (FR-054), and into `failed` would hide the fact that most of the corpus
    indexed fine.
    """

    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    COMPLETED_WITH_ERRORS = "completed_with_errors"
    FAILED = "failed"
    CANCELLED = "cancelled"


# ============================================================================
# Sources
# ============================================================================


class SourceCreate(ApiModel):
    """`POST /sources`.

    `start_crawl` defaults to true because registering a source and leaving it
    un-crawled is almost never what the operator meant, and the alternative is a
    second round trip that explains nothing when it goes wrong. `false` is
    available for registering a source ahead of a maintenance window.
    """

    name: str = Field(min_length=1, max_length=200)
    start_url: HttpUrl
    product_domain: str | None = Field(
        default=None,
        max_length=100,
        description="Null for a cross-product source (FR-013). Never forced.",
    )
    max_pages: int = Field(default=100, ge=1, le=500)
    max_depth: int = Field(default=2, ge=0, le=5)
    delay_seconds: float = Field(default=1.0, ge=0.1, le=60.0)
    start_crawl: bool = True


class SourceUpdate(ApiModel):
    """`PATCH /sources/{source_id}`. Every field optional, none required.

    A body of `{}` is legal and changes nothing, which is why the route reads
    `model_fields_set` rather than the field values: an update that sets nothing
    must not write a row claiming that it did.
    """

    name: str | None = Field(default=None, min_length=1, max_length=200)
    enabled: bool | None = None
    product_domain: str | None = Field(default=None, max_length=100)
    max_pages: int | None = Field(default=None, ge=1, le=500)
    max_depth: int | None = Field(default=None, ge=0, le=5)
    delay_seconds: float | None = Field(default=None, ge=0.1, le=60.0)


class SourceOut(ApiModel):
    """A registered source with derived counts.

    `page_count` and `unit_count` are counted on read, never stored
    (data-model.md §2.1). `last_crawl_status` is the status of the most recent
    job, which is null when the source has never been crawled — a source with no
    crawl is not a source in `error`.
    """

    id: UUID
    name: str
    start_url: str
    allowed_domains: list[str]
    product_domain: str | None = None
    enabled: bool
    status: SourceStatus
    page_count: int = Field(ge=0, description="Live documents; tombstones excluded.")
    unit_count: int = Field(ge=0)
    last_crawl_at: datetime | None = None
    last_crawl_status: CrawlJobStatus | None = None
    created_at: datetime


# ============================================================================
# Documents and units
# ============================================================================


class DocumentOut(ApiModel):
    """One ingested page.

    `state_detail` is the state's `code` — the reason vocabulary — rather than the
    stored detail object, because the contract types it as a string. The full
    detail is in the log keyed by document id; a field reproducing it here would
    be a second, unpaged place for measured content to accumulate.
    """

    id: UUID
    source_id: UUID
    url: str
    canonical_url: str | None = None
    title: str | None = None
    product: str | None = None
    category: str | None = None
    page_type: str | None = None
    language: str | None = None
    state: DocumentState
    state_detail: str | None = None
    vectors_live: bool
    unit_count: int = Field(ge=0)
    content_fingerprint: str | None = None
    source_modified_at: datetime | None = None
    last_crawled_at: datetime | None = None
    indexed_at: datetime | None = None
    tombstoned_at: datetime | None = None
    created_at: datetime


class DocumentUnitOut(ApiModel):
    """Provenance for one indexed unit. **No text field exists here.**"""

    id: UUID
    document_id: UUID
    ordinal: int = Field(ge=0)
    vector_id: str
    heading_path: list[str]
    block_types: list[str]
    token_count: int = Field(ge=1)
    overlap_tokens: int = Field(default=0, ge=0)
    text_fingerprint: str


# ============================================================================
# Crawl jobs
# ============================================================================


class CrawlJobCounters(ApiModel):
    """The five counters the contract declares, and only those."""

    discovered: int = Field(ge=0)
    processed: int = Field(ge=0)
    unchanged: int = Field(ge=0, description="Pages whose fingerprint matched; not re-indexed.")
    skipped: int = Field(ge=0)
    failed: int = Field(ge=0)


class CrawlJobOut(ApiModel):
    """A crawl job, including why it did less than it discovered.

    `error_code` is one of the fourteen stable codes; `error_message` is the
    bounded, sanitised message from `_safe_message`, never a vendor body or a
    traceback (FR-047).
    """

    id: UUID
    source_id: UUID
    target_url: str
    status: CrawlJobStatus
    requested_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    counters: CrawlJobCounters
    skipped_reasons: dict[str, int] | None = None
    error_code: str | None = None
    error_message: str | None = None


# ============================================================================
# Composite responses
# ============================================================================


class SourceRegistered(ApiModel):
    """`POST /sources` on a new source: the source, and the crawl it started.

    `crawl_job` is null when the request set `start_crawl: false` — amended into
    the contract on 2026-10-03 for the reason recorded there. The short version:
    the only job a crawl-less registration could invent is a `queued` one, and a
    queued job holds the active-crawl slot, so the operator's next explicit crawl
    request would be refused against a crawl that never began.
    """

    source: SourceOut
    crawl_job: CrawlJobOut | None = None


class SourcePage(Page):
    items: list[SourceOut]


class DocumentPage(Page):
    items: list[DocumentOut]


class DocumentUnitPage(Page):
    items: list[DocumentUnitOut]


class CrawlJobPage(Page):
    items: list[CrawlJobOut]


__all__ = [
    "CrawlJobCounters",
    "CrawlJobOut",
    "CrawlJobPage",
    "CrawlJobStatus",
    "DocumentOut",
    "DocumentPage",
    "DocumentState",
    "DocumentUnitOut",
    "DocumentUnitPage",
    "SourceCreate",
    "SourceOut",
    "SourcePage",
    "SourceRegistered",
    "SourceStatus",
    "SourceUpdate",
]
