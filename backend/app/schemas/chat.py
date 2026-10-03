"""Chat request/response schemas (T089), in the contract's vocabulary.

All of these are closed, exactly like the generated sources models: the payload
shape is the contract, extra keys are refused at the boundary, and a null
conditional is *different* from an absent one because the contract declares
`type: [string, 'null']` specifically. The classes are a PyDantic form of the
`AskResponse` envelope; anything that does not validate is a server bug, which
the error handler reports rather than a client that received garbage.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from app.schemas import ApiModel


class HistoryMessage(ApiModel):
    role: Literal["user", "assistant"]
    content: str = Field(max_length=8000)


class AskRequest(ApiModel):
    question: str = Field(min_length=1, max_length=2000)
    inspect: bool = False
    history: list[HistoryMessage] = Field(default_factory=list, max_length=10)


class SearchedScope(ApiModel):
    queries: list[str]
    products: list[str | None]
    applied_filters: dict[str, Any] = Field(default_factory=dict)
    candidates_retrieved: int = Field(ge=0)
    candidates_reranked: int = Field(ge=0)
    evidence_selected: int = Field(ge=0)


class Lead(ApiModel):
    source_url: str
    title: str
    heading_path: list[str] = Field(default_factory=list)
    relevance: float = Field(ge=0.0, le=1.0)
    insufficient: Literal[True] = True


class CitationOut(ApiModel):
    rank: int = Field(ge=1)
    document_id: str
    unit_id: str
    source_url: str
    title: str
    product: str | None = None
    category: str | None = None
    heading_path: list[str] = Field(default_factory=list)
    quote: str = ""
    retrieval_score: float | None = None
    rerank_score: float | None = None
    validation_state: Literal["valid", "stripped", "repaired"]
    validation_note: str | None = None


class TimingOut(ApiModel):
    total_ms: int = Field(ge=0)
    first_event_ms: int | None = Field(default=None, ge=0)
    stages_ms: dict[str, int] = Field(default_factory=dict)


class QueryAnalysisOut(ApiModel):
    original: str
    normalized: str | None = None
    detected_product: str | None = None
    detected_category: str | None = None
    intent: str | None = None
    confidence: dict[str, float] = Field(default_factory=dict)
    entities: list[str] = Field(default_factory=list)
    rewrite_queries: list[str] = Field(default_factory=list)
    ambiguity_note: str | None = None


class PipelineTraceOut(ApiModel):
    """Free-form because every stage can extend it; reasoning is never present."""

    request_id: str | None = None
    retrieval: dict[str, Any] | None = None
    reranking: dict[str, Any] | None = None
    generation: dict[str, Any] | None = None
    citations: dict[str, Any] | None = None


class AskResponse(ApiModel):
    request_id: str
    question: str
    outcome: Literal["answered", "refused"]
    answer: str | None = None
    refusal_reason: Literal[
        "INSUFFICIENT_EVIDENCE", "UNSUPPORTED_INTENT", "TOO_AMBIGUOUS", "GENERATION_FAILED"
    ] | None = None
    searched: SearchedScope | None = None
    leads: list[Lead] = Field(default_factory=list)
    citations: list[CitationOut] = Field(default_factory=list)
    query_analysis: QueryAnalysisOut | None = None
    timing: TimingOut
    trace: PipelineTraceOut | None = None
