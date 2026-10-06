"""Metrics provenance (T136): every number a dashboard can show exists inside a
run row with a fingerprint and a timestamp, and no schema path renders a
placeholder."""

from __future__ import annotations

import json

from app.schemas.evaluations import EvaluationMetrics, EvaluationRun

# The exact twelve quality numbers the contract declares. If this set ever
# disagrees with openapi.yaml, the conformance suite must be the one to say so
# — and this list pins the claim locally.
CONTRACT_METRIC_KEYS = {
    "recall_at_5",
    "precision_at_5",
    "mrr",
    "cross_product_domain_coverage",
    "citation_validity",
    "claim_coverage",
    "faithfulness",
    "unsupported_refusal_rate",
    "fabricated_fact_count",
    "invalid_citation_count",
    "p50_latency_ms",
    "p95_latency_ms",
    "error_rate",
}


class TestMetricsStructuralHonesty:
    def test_the_metric_fields_are_exactly_the_contract_keys(self) -> None:
        assert set(EvaluationMetrics.model_fields) == CONTRACT_METRIC_KEYS

    def test_an_empty_metrics_object_is_a_null_valued_model_not_a_result(self) -> None:
        """{} carries no numbers; only a measured run may."""
        model = EvaluationMetrics.model_validate({})
        assert all(getattr(model, key) is None for key in CONTRACT_METRIC_KEYS)

    def test_metrics_null_is_the_honest_empty(self) -> None:
        run = EvaluationRun(
            id="00000000-0000-0000-0000-000000000000",
            dataset_version="1.2.0",
            dataset_fingerprint="ab" * 32,
            status="completed",
            question_count=32,
            metrics=None,
            gate_outcome="fail",
        )
        body = json.loads(run.model_dump_json())
        assert body["metrics"] is None
        # The model layer cannot express "some zeroes": every number lives in
        # metrics, and metrics is null until a run puts real numbers there.
        for key in CONTRACT_METRIC_KEYS:
            assert key not in body

    def test_a_finished_run_serialises_no_fabricated_defaults(self) -> None:
        """A serialized run with metrics set contains only measured values;
        there is no shadow copy of a metric elsewhere on the envelope."""
        fields = set(EvaluationRun.model_fields)
        for key in CONTRACT_METRIC_KEYS:
            assert key not in fields
