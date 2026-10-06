"""A question that spans two products (T098 / edge case 13).

Three properties, and the second is the one that matters most:

1. **Both products are searched.** A cross-product question retrieves evidence
   from every domain it spans, not from whichever one the classifier ranked first.
2. **The ambiguity is stated.** `ambiguity_note` names the products, and the
   response's `searched.products` lists what was covered — so a reader who gets an
   answer from Jira alone learns that Confluence was searched too.
3. **Nothing is silently committed to one domain.** No product filter is applied,
   and the analyser's single answer is not presented as *the* answer to a
   two-product question.

These are unit tests over the detector, the filter, and the response fields. The
retrieval itself is covered by T114's per-aspect queries; what is pinned here is
that the pipeline never *narrows* a cross-product question in the first place.
"""

from __future__ import annotations

from typing import Any

import pytest

pytestmark = pytest.mark.unit


class _FakeProvider:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload

    async def classify(self, *, system: str, user: str) -> dict[str, Any]:
        return self.payload

    async def complete(self, **_kwargs: object):  # pragma: no cover - never called here
        raise AssertionError("the analysis tests must not generate")

    async def list_models(self) -> list[str]:
        return []


def _confident_in(product: str) -> _FakeProvider:
    """The analyser's most confident possible answer about one product."""
    return _FakeProvider(
        {
            "product": product,
            "category": "rest-api",
            "intent": "comparison",
            "entities": ["template"],
            "confidence": {"product": 0.99, "category": 0.95, "intent": 0.98, "entities": 0.9},
        }
    )


class TestProductDetection:
    @pytest.mark.parametrize(
        ("question", "expected"),
        [
            ("Can I share a template between Jira and Confluence?", ["jira", "confluence"]),
            ("How do I use the Jira Cloud REST API?", ["jira", "developer"]),
            ("What is the capital of France?", []),
            ("How do I create a JSM request?", ["jsm"]),
            ("Jira Service Desk versus Confluence", ["jsm", "confluence"]),
        ],
    )
    def test_products_are_named_in_order_of_appearance(
        self, question: str, expected: list[str]
    ) -> None:
        from app.retrieval.ambiguity import products_named

        assert products_named(question) == expected

    def test_developer_documentation_is_not_a_second_product(self) -> None:
        """`/cloud/jira/platform/` is Jira's own developer section.

        Counting both would declare almost every question in this corpus ambiguous,
        and an ambiguity note that is nearly always wrong stops being read.
        """
        from app.retrieval.ambiguity import resolve_ambiguity

        decision = resolve_ambiguity("How do I authenticate a Jira Cloud REST API request?", "jira")

        assert decision.ambiguous is False
        assert decision.note is None

    def test_a_product_name_inside_a_longer_word_is_not_a_product(self) -> None:
        from app.retrieval.ambiguity import products_named

        assert products_named("How do I capitalise a field in a Jira workflow?") == ["jira"]

    def test_a_single_product_is_not_ambiguous(self) -> None:
        from app.retrieval.ambiguity import resolve_ambiguity

        assert resolve_ambiguity("How do I rotate a Jira API token?", "jira").ambiguous is False


class TestTheAnalysisStatesTheAmbiguity:
    @pytest.mark.asyncio
    async def test_a_cross_product_question_reports_both_products(self) -> None:
        from app.retrieval.query_analyzer import analyze_question

        analysis = await analyze_question(
            "Can I share a template between Jira and Confluence?",
            provider=_confident_in("jira"),
        )

        assert analysis.ambiguity_note is not None
        assert "jira" in analysis.ambiguity_note
        assert "confluence" in analysis.ambiguity_note
        assert analysis.named_products == ["jira", "confluence"]

    @pytest.mark.asyncio
    async def test_a_confident_classifier_does_not_dismiss_the_ambiguity(self) -> None:
        """Confidence in one product is not evidence the question meant one."""
        from app.retrieval.query_analyzer import analyze_question

        analysis = await analyze_question(
            "Can I share a template between Jira and Confluence?",
            provider=_confident_in("jira"),
        )

        assert analysis.classification_confidence["product"] >= 0.6
        assert analysis.ambiguity_note is not None

    @pytest.mark.asyncio
    async def test_an_ordinary_question_reports_no_ambiguity(self) -> None:
        from app.retrieval.query_analyzer import analyze_question

        analysis = await analyze_question(
            "How do I rotate a Jira API token?", provider=_confident_in("jira")
        )

        assert analysis.ambiguity_note is None


class TestTheFilterDoesNotNarrowAcrossProducts:
    @pytest.mark.asyncio
    async def test_a_cross_product_analysis_applies_no_product_filter(self) -> None:
        from app.retrieval.query_analyzer import analyze_question
        from app.retrieval.retriever import build_metadata_filter

        analysis = await analyze_question(
            "Can I share a template between Jira and Confluence?",
            provider=_confident_in("jira"),
        )
        plan = build_metadata_filter(analysis)

        assert plan.suppressed is True
        assert plan.filter is None
        assert plan.reason == "ambiguous"

    @pytest.mark.asyncio
    async def test_an_ordinary_analysis_still_filters(self) -> None:
        """The suppression is specific: it does not switch filtering off."""
        from app.retrieval.query_analyzer import analyze_question
        from app.retrieval.retriever import build_metadata_filter

        analysis = await analyze_question(
            "How do I rotate a Jira API token?", provider=_confident_in("jira")
        )
        plan = build_metadata_filter(analysis)

        assert plan.suppressed is False
        assert plan.filter == {"product": "jira", "category": "rest-api"}


class TestTheResponseSaysWhatWasSearched:
    def test_the_searched_scope_lists_every_named_product(self) -> None:
        """FR-008 applied to ambiguity: the scope says what the search covered."""
        from app.chat.service import searched_products
        from app.retrieval.query_analyzer import QueryAnalysis

        analysis = QueryAnalysis(
            original_question="q",
            detected_product="jira",
            detected_category=None,
            intent="comparison",
            named_products=["jira", "confluence"],
            ambiguity_note="This question mentions jira and confluence, so it was searched across both.",
        )

        assert searched_products(analysis) == ["jira", "confluence"]

    def test_an_ordinary_search_reports_the_one_product(self) -> None:
        from app.chat.service import searched_products
        from app.retrieval.query_analyzer import QueryAnalysis

        analysis = QueryAnalysis(
            original_question="q",
            detected_product="jira",
            detected_category=None,
            intent="how_to",
        )

        assert searched_products(analysis) == ["jira"]

    def test_an_unknown_product_is_reported_as_null_not_omitted(self) -> None:
        """`null` and absent mean different things, and the scope needs both."""
        from app.chat.service import searched_products
        from app.retrieval.query_analyzer import QueryAnalysis

        analysis = QueryAnalysis(
            original_question="q",
            detected_product=None,
            detected_category=None,
            intent="unknown",
        )

        assert searched_products(analysis) == [None]
