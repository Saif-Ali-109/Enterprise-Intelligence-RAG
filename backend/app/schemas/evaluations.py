"""Evaluation API schemas (T147), matching contracts/openapi.yaml exactly.

The shapes are closed (`ApiModel` refuses extras), so a drift between this file
and `openapi.yaml` fails the conformance suite rather than shipping silently.
`metrics` is `null` until a run finishes, and "no run" is not "all zeros" —
FR-036 exists to keep a dashboard from ever presenting a blank as a result.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import Field

from app.schemas import ApiModel


class EvaluationDatasetQuestionExpected(ApiModel):
    must_refuse: bool
    product: str | None = None
    category: str | None = None
    heading_path: list[str] = Field(default_factory=list)
    topics: list[str] = Field(min_length=1)
    notes: str | None = None


class EvaluationDatasetQuestion(ApiModel):
    id: str
    question: str
    category: Literal[
        "simple_lookup",
        "multi_part",
        "cross_product",
        "troubleshooting",
        "permissions",
        "api",
        "ambiguous",
        "unsupported",
    ]
    difficulty: Literal["easy", "medium", "hard"]
    is_unsupported: bool
    expected: EvaluationDatasetQuestionExpected


class EvaluationDataset(ApiModel):
    dataset_version: str
    generated_at: datetime | None = None
    questions: list[EvaluationDatasetQuestion] = Field(min_length=30)


class EvaluationGateFailure(ApiModel):
    gate: str
    target: float
    actual: float


class EvaluationMetrics(ApiModel):
    recall_at_5: float | None = None
    precision_at_5: float | None = None
    mrr: float | None = None
    cross_product_domain_coverage: float | None = None
    citation_validity: float | None = None
    claim_coverage: float | None = None
    faithfulness: float | None = None
    unsupported_refusal_rate: float | None = None
    fabricated_fact_count: int | None = None
    invalid_citation_count: int | None = None
    p50_latency_ms: int | None = None
    p95_latency_ms: int | None = None
    error_rate: float | None = None


class EvaluationRun(ApiModel):
    id: str
    dataset_version: str
    dataset_fingerprint: str
    status: Literal["queued", "running", "completed", "failed"]
    started_at: datetime | None = None
    finished_at: datetime | None = None
    question_count: int = 0
    metrics: EvaluationMetrics | None = None
    thresholds: dict[str, float] = Field(default_factory=dict)
    gate_outcome: Literal["pass", "fail"]
    gate_failures: list[EvaluationGateFailure] = Field(default_factory=list)


class EvaluationResult(ApiModel):
    question_id: str
    question: str | None = None
    category: str | None = None
    difficulty: str | None = None
    recall_at_5: float | None = None
    precision_at_5: float | None = None
    mrr: float | None = None
    citation_validity: float | None = None
    claim_coverage: float | None = None
    faithfulness: float | None = None
    refused: bool
    fabricated_fact_count: int
    invalid_citation_count: int
    latency_ms: int | None = None
    error: str | None = None


class EvaluationRunPage(ApiModel):
    items: list[EvaluationRun]
    total: int
    limit: int
    offset: int


class EvaluationResultPage(ApiModel):
    items: list[EvaluationResult]
    total: int
    limit: int
    offset: int


# The runner's internal result is typed on the DB row; keep the dict-any boundary
# out of the API layer by making the contract shape the only way out.
ResultRow = dict[str, Any]
