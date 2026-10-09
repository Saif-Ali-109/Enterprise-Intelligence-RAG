"""Gate thresholds: recorded, a raised gate names its failure, and the two
absolute-zero gates are counts with no configuration (T137)."""

from __future__ import annotations

from app.evaluation.runner import gate_outcome

# A metrics dict where every gate value clears its threshold and the zero
# counters are zero. Individual keys are cut out in the tests.
GOOD_METRICS = {
    "recall_at_5": 0.90,
    "precision_at_5": 0.80,
    "mrr": 0.85,
    "cross_product_domain_coverage": 0.90,
    "citation_validity": 1.0,
    "claim_coverage": 0.95,
    "faithfulness": 0.95,
    "unsupported_refusal_rate": 1.0,
    "p95_latency_ms": 5000,
    "fabricated_fact_count": 0,
    "invalid_citation_count": 0,
}

THRESHOLDS = {
    "recall_at_5": 0.80,
    "precision_at_5": 0.60,
    "mrr": 0.75,
    "cross_product_coverage": 0.80,
    "citation_validity": 0.98,
    "claim_coverage": 0.85,
    "faithfulness": 0.90,
    "unsupported_refusal_rate": 1.00,
    "p95_latency_ms": 8000.0,
}


class TestGateOutcome:
    def test_everything_passing_is_a_pass(self) -> None:
        outcome, failures = gate_outcome(GOOD_METRICS, THRESHOLDS)
        assert outcome == "pass"
        assert failures == []

    def test_a_raised_threshold_names_the_failing_gate(self) -> None:
        thresholds = dict(THRESHOLDS)
        thresholds["precision_at_5"] = 0.99
        outcome, failures = gate_outcome(GOOD_METRICS, thresholds)
        assert outcome == "fail"
        by_gate = {f["gate"]: f for f in failures}
        assert "precision_at_5" in by_gate
        assert by_gate["precision_at_5"]["target"] == 0.99
        assert by_gate["precision_at_5"]["actual"] == GOOD_METRICS["precision_at_5"]

    def test_latency_gate_is_the_only_at_most(self) -> None:
        slow = dict(GOOD_METRICS)
        slow["p95_latency_ms"] = 9000
        outcome, failures = gate_outcome(slow, THRESHOLDS)
        assert outcome == "fail"
        assert [f["gate"] for f in failures] == ["p95_latency_ms"]

    def test_fabricated_facts_fail_regardless_of_any_threshold(self) -> None:
        poisoned = dict(GOOD_METRICS)
        poisoned["fabricated_fact_count"] = 3
        # No threshold entry exists for the zero gates — they cannot be tuned.
        assert "fabricated_fact_count" not in THRESHOLDS
        outcome, failures = gate_outcome(poisoned, THRESHOLDS)
        assert outcome == "fail"
        by_gate = {f["gate"]: f for f in failures}
        assert by_gate["fabricated_fact_count"]["target"] == 0
        assert by_gate["fabricated_fact_count"]["actual"] == 3

    def test_invalid_citations_fail_regardless_of_any_threshold(self) -> None:
        poisoned = dict(GOOD_METRICS)
        poisoned["invalid_citation_count"] = 2
        assert "invalid_citation_count" not in THRESHOLDS
        outcome, failures = gate_outcome(poisoned, THRESHOLDS)
        assert outcome == "fail"
        assert any(f["gate"] == "invalid_citation_count" for f in failures)

    def test_a_metric_missing_from_the_run_is_null_not_a_fabricated_failure(self) -> None:
        # FR-041/FR-036: a gate that never produced a number cannot fail a run
        # with a synthetic zero — its absence is the null metric on the row.
        thin = dict(GOOD_METRICS)
        del thin["faithfulness"]
        outcome, failures = gate_outcome(thin, THRESHOLDS)
        assert outcome == "pass"
        assert failures == []
