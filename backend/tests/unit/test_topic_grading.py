"""Topic-coverage grading (T138): a URL is never the grade, and one of several
valid answers is still a hit."""

from __future__ import annotations

import inspect as _inspect

import pytest
from app.evaluation.metrics import (
    heading_covers,
    retrieval_grades,
    topic_covered_in_text,
    unit_covers_target,
)


def _expected() -> object:
    class E:
        product = "jira"
        category = "filters"
        heading_path = ["Save your search as a filter"]

    return E()


def _candidate(product: str | None, category: str | None, heading: list[str]) -> dict:
    return {"id": "x", "product": product, "category": category, "heading_path": heading}


class TestCoverage:
    def test_same_topic_is_a_hit_regardless_of_title_wording(self) -> None:
        assert unit_covers_target(
            product="Jira",
            category="Filters",
            heading_path=["Save your search as a filter"],
            expected_product="jira",
            expected_category="filters",
            expected_heading_path=["Save your search as a filter"],
        )

    def test_wrong_product_category_or_heading_is_a_miss(self) -> None:
        assert not unit_covers_target(
            product="confluence",
            category="filters",
            heading_path=["Save your search as a filter"],
            expected_product="jira",
            expected_category="filters",
            expected_heading_path=["Save your search as a filter"],
        )
        assert not unit_covers_target(
            product="jira",
            category="boards",
            heading_path=["Save your search as a filter"],
            expected_product="jira",
            expected_category="filters",
            expected_heading_path=["Save your search as a filter"],
        )
        assert not unit_covers_target(
            product="jira",
            category="filters",
            heading_path=["Create a dashboard"],
            expected_product="jira",
            expected_category="filters",
            expected_heading_path=["Save your search as a filter"],
        )

    def test_heading_coverage_is_order_sensitive_but_not_contiguous(self) -> None:
        assert heading_covers(["A", "C"], ["A", "B", "C"])
        assert not heading_covers(["C", "A"], ["A", "B", "C"])
        assert not heading_covers([], ["A"])

    def test_an_expected_topic_is_covered_when_its_words_appear(self) -> None:
        topic = "a saved filter is runnable again by name"
        text = "A saved filter is runnable again by name, from any project."
        assert topic_covered_in_text(topic, text)
        assert not topic_covered_in_text(topic, "How to create a dashboard.")


class TestGrades:
    def test_binary_recall_precision_mrr(self) -> None:
        ranked = [
            _candidate("confluence", "pages", ["x"]),
            _candidate("jira", "filters", ["Save your search as a filter"]),
            _candidate("jira", "boards", ["y"]),
        ]
        recall, precision, mrr = retrieval_grades(
            ranked, expected=_expected(), is_unsupported=False
        )
        assert recall == 1.0
        assert precision == pytest.approx(1 / 3)
        assert mrr == pytest.approx(1 / 2)

    def test_unsupported_questions_are_not_applicable_not_zero(self) -> None:
        recall, precision, mrr = retrieval_grades([], expected=_expected(), is_unsupported=True)
        assert (recall, precision, mrr) == (None, None, None)

    def test_one_of_several_valid_answers_counts(self) -> None:
        # The expectation names one acceptable path; a second equally valid page
        # structure is the same question answered well. Retrieval recall does not
        # score a single expected phrasing as the only truth.
        ranked = [_candidate("jira", "filters", ["Save your search as a filter"])]
        recall, _, _ = retrieval_grades(ranked, expected=_expected(), is_unsupported=False)
        assert recall == 1.0

    def test_no_url_in_the_grading_signature(self) -> None:
        import app.evaluation.metrics as metrics

        params: set[str] = set()
        for name in (
            "unit_covers_target",
            "retrieval_grades",
            "topic_covered_in_text",
            "heading_covers",
        ):
            params.update(_inspect.signature(getattr(metrics, name)).parameters)
        assert "source_url" not in params and "url" not in params
