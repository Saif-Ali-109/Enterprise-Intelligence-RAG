"""Shared Pydantic v2 base types and the system-response schemas.

`contracts/openapi.yaml` is the authority for every shape here. Two rules from
that file are enforced structurally rather than by review:

**Closed where the contract is closed.** `EXTRA_FORBIDDEN` is the default for
every schema in this package. A model with a permissive `extra` would let a
field appear in a response that the contract does not describe, and the contract
suite would not catch it — because the suite validates what the schema says, and
the schema would have been widened to match. Making `extra="forbid"` the default
means the failure appears in the response model rather than in a review.

**No reasoning field, anywhere.** FR-034 forbids exposing model reasoning. It is
easy to satisfy that today by not writing such a field, and easy to break
tomorrow by adding one for debugging. Because every schema here is closed, an
added `reasoning` field is a validation failure in the contract suite rather than
a new field in an API response — the leak check in `tests/contract/
test_openapi_conformance.py` greps the payloads for exactly this.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# ============================================================================
# Bases
# ============================================================================

#: Applied to every schema in this package. See the module docstring.
EXTRA_FORBIDDEN = ConfigDict(extra="forbid", populate_by_name=True)


class ApiModel(BaseModel):
    """Base for every request and response schema.

    `use_enum_values` is NOT set: enums are kept as enum members so a handler
    cannot accidentally serialise `ProductDomain.JIRA` where the contract says
    `"jira"`, and so an `IntEnum`-style comparison cannot silently succeed. The
    response serialiser handles the conversion at the boundary.
    """

    model_config = EXTRA_FORBIDDEN


class Page(ApiModel):
    """Offset pagination envelope.

    Offset rather than cursor because the corpus is bounded at 50–100 pages
    (contracts/README.md, Pagination). Cursor pagination would add a token to
    maintain and a failure mode where a record inserted mid-scroll silently
    shifts the window, for a collection that fits in memory.
    """

    items: list[Any] = Field(description="The page of results.")
    total: int = Field(ge=0, description="Total matching records, not the page length.")
    limit: int = Field(ge=1, le=200, description="Page size requested.")
    offset: int = Field(ge=0, description="Zero-based index of the first item returned.")


# ============================================================================
# System
# ============================================================================


class DependencyStatus(StrEnum):
    """Per-dependency health. Reported independently, not collapsed."""

    OK = "ok"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"


class DependencyReport(ApiModel):
    """One dependency's state.

    `detail` is safe to display: it is an authored phrase, never a driver error
    string, a connection URL, or a stack trace (FR-047).
    """

    status: DependencyStatus
    detail: str | None = None
    latency_ms: int | None = Field(default=None, ge=0)


class HealthResponse(ApiModel):
    """`GET /health`.

    The three dependencies report independently so a vector outage is visible as
    a vector outage. A single rolled-up `status` alone would present a
    `VECTOR_SERVICE_UNAVAILABLE` condition as a generic "the service is
    unhealthy", and the operator would have no idea which vendor to look at.
    """

    status: DependencyStatus
    version: str | None = None
    dependencies: dict[str, DependencyReport]


class SecretPresence(ApiModel):
    """Whether a credential is configured. **There is no value field.**

    `additionalProperties: false` in the contract, and it is repeated here: a
    `value` attribute on this class would let a stray `asdict()` publish a
    credential, and the shape is what prevents that (FR-042).
    """

    configured: bool


class RetrievalConfig(ApiModel):
    candidate_pool: int = Field(ge=1)
    rerank_top_n: int = Field(ge=1)
    evidence_min: int = Field(ge=1)
    evidence_max: int = Field(ge=1)
    min_rerank_score: float | None = Field(default=None, ge=0.0, le=1.0)
    min_evidence_score: float | None = Field(default=None, ge=0.0, le=1.0)
    filters_enabled: bool


class GenerationConfig(ApiModel):
    provider: str
    model: str
    classification_model: str | None = None
    max_attempts: int = Field(ge=1)
    #: `const: false`. Present so an operator can verify FR-034 against the
    #: running system rather than take the specification's word for it.
    reasoning_exposed: Literal[False] = False


class IndexConfig(ApiModel):
    name: str
    namespace: str
    embed_model: str
    #: `const: 'service'` — the claim is that the dimension is read from service
    #: configuration and never hard-coded (Principle VII), and this makes the
    #: claim checkable from outside.
    dimension_source: Literal["service"] = "service"
    rerank_model: str | None = None


class CorpusSummary(ApiModel):
    source_count: int = Field(ge=0)
    document_count: int = Field(ge=0)
    unit_count: int = Field(ge=0)
    allowed_domains: list[str]


class SecretsReport(ApiModel):
    """Presence only. No entry has a value field, at any depth (FR-042)."""

    pinecone_api_key: SecretPresence = SecretPresence(configured=False)
    groq_api_key: SecretPresence = SecretPresence(configured=False)
    database_url: SecretPresence = SecretPresence(configured=False)


class ConfigResponse(ApiModel):
    """`GET /config`. Effective configuration, with no secret values."""

    retrieval: RetrievalConfig
    generation: GenerationConfig
    index: IndexConfig
    corpus: CorpusSummary
    secrets: SecretsReport


# ============================================================================
# Reusable
# ============================================================================

Uuid4Str = Annotated[
    str,
    Field(
        pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
        description="Opaque UUIDv4 string. Clients must not parse it (contracts/README.md).",
    ),
]

#: RFC 3339 with an explicit offset, UTC.
TimestampStr = Annotated[
    str,
    Field(
        pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$",
        description="RFC 3339, UTC.",
    ),
]

#: A score in [0, 1]. Constrained, because a score outside the range means the
#: thing producing it is miscalibrated and a silent clamp would hide that.
Score = Annotated[float, Field(ge=0.0, le=1.0)]


__all__ = [
    "ApiModel",
    "ConfigResponse",
    "CorpusSummary",
    "DependencyReport",
    "DependencyStatus",
    "EXTRA_FORBIDDEN",
    "GenerationConfig",
    "HealthResponse",
    "IndexConfig",
    "Page",
    "RetrievalConfig",
    "Score",
    "SecretPresence",
    "SecretsReport",
    "TimestampStr",
    "Uuid4Str",
]
