"""Filter construction: when to narrow, and when to widen (T074 / FR-010, FR-014, FR-052).

The two failure directions are not equal. A filter that is too narrow *silently*
removes the evidence a confident answer would need — the user just gets a
refusal and cannot tell why. A filter that is too broad costs a noisier pool,
which the reranker and the evidence selector are already paid to deal with. So
the safe side of being wrong is always the wider side.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


def _analysis(product=None, category=None, intent=None, product_conf=0.0, category_conf=0.0, intent_conf=0.0):
    from app.retrieval.query_analyzer import QueryAnalysis

    return QueryAnalysis(
        original_question="x",
        detected_product=product,
        detected_category=category,
        intent=intent,
        entities=[],
        classification_confidence={
            "product": product_conf,
            "category": category_conf,
            "intent": intent_conf,
            "entities": 0.0,
        },
    )


class TestFilterConstruction:
    def test_high_confidence_fields_become_filters(self) -> None:
        from app.retrieval.retriever import build_metadata_filter

        plan = build_metadata_filter(
            _analysis(product="jira", category="projects", product_conf=0.95, category_conf=0.9)
        )
        assert plan.suppressed is False
        assert plan.filter == {"product": "jira", "category": "projects"}

    def test_low_confidence_product_is_not_a_filter(self) -> None:
        from app.retrieval.retriever import build_metadata_filter

        plan = build_metadata_filter(
            _analysis(product="confluence", product_conf=0.4, category_conf=0.95, category="spaces")
        )
        # The product guess is discarded; the search must not narrow on it.
        assert plan.filter is None or "product" not in plan.filter

    def test_when_every_field_is_low_confidence_the_whole_filter_is_suppressed(self) -> None:
        from app.retrieval.retriever import build_metadata_filter

        plan = build_metadata_filter(_analysis(product="jira", product_conf=0.3, category_conf=0.2))
        assert plan.suppressed is True
        assert plan.filter is None

    def test_threshold_equality_is_honoured(self) -> None:
        from app.retrieval.retriever import build_metadata_filter

        plan = build_metadata_filter(_analysis(product="jira", product_conf=0.6))
        assert plan.filter == {"product": "jira"}

    def test_a_cross_product_analysis_yields_no_product_filter(self) -> None:
        from app.retrieval.retriever import build_metadata_filter

        plan = build_metadata_filter(_analysis(product_conf=0.3, category_conf=0.3))
        assert plan.suppressed is True

    def test_filters_disabled_means_suppressed(self) -> None:
        from app.retrieval.retriever import build_metadata_filter

        plan = build_metadata_filter(
            _analysis(product="jira", product_conf=0.99), filters_enabled=False
        )
        assert plan.suppressed is True
        assert plan.filter is None

    def test_the_plan_states_its_reason(self) -> None:
        from app.retrieval.retriever import build_metadata_filter

        plan = build_metadata_filter(_analysis(product="jira", product_conf=0.99))
        assert plan.reason in ("confident", "narrowed")
        plan2 = build_metadata_filter(_analysis())
        assert plan2.reason in ("low_confidence", "disabled")
