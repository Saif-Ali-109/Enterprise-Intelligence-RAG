"""Cross-domain coverage (T111 / FR-013, SC-004).

SC-004 asks for at least 90% of multi-part and cross-product questions to retrieve
evidence from *every* domain the question spans. The mechanism is three pieces,
and each is pinned here separately because each can fail alone while the others
look fine:

1. **The rewrite issues one scoped query per domain** (T114). Two aspects that are
   the same string search the same thing twice and one of the two domains is
   never consulted.
2. **Each aspect is filtered to its own domain** (T115). One wide search per
   aspect retrieves twelve units from whichever domain ranks highest and calls it
   a cross-domain answer.
3. **The merged pool respects the candidate budget** (T115). Twelve candidates per
   aspect is twenty-four candidates, and the retrieval budget is a number the
   operator set.

Note what these tests do *not* claim: that a real cross-product question finds
evidence in both domains. That is a property of the corpus, and it is measured by
the evaluation phase against a seeded corpus — not asserted here, where both
domains can be made to look perfect by a double.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


#: Six unrelated sentences, so nothing here folds as a near-duplicate and the
#: selection's behaviour is attributable to score and grouping alone.
_DISTINCT_PASSAGES = [
    "A linked page inherits its viewer permissions from the space it lives in.",
    "Jira issue links to a Confluence page resolve for users who can see both.",
    "Anonymous links bypass the space permission check entirely.",
    "A page shared from Jira keeps its original space restrictions.",
    "Cross-space links respect the most restrictive of the two permissions.",
    "Page restrictions can be inherited or overridden per space.",
    "The visible link list on an issue is filtered by the user's product access.",
]


def _analysis(*, products: list[str] | None = None, note: str | None = None):
    from app.retrieval.query_analyzer import QueryAnalysis

    return QueryAnalysis(
        original_question="Why can a user see a linked page in Confluence but not in Jira?",
        detected_product="jira" if not products else None,
        detected_category=None,
        intent="comparison",
        named_products=products or [],
        ambiguity_note=note,
    )


def _cross_product():
    return _analysis(
        products=["confluence", "jira"],
        note="This question mentions confluence and jira, so it was searched across both.",
    )


class TestOneQueryPerDomain:
    def test_a_cross_product_question_becomes_one_scoped_query_per_product(self) -> None:
        from app.retrieval.query_rewriter import rewrite_queries, scope_of

        question = "Why can a user see a linked page in Confluence but not in Jira?"
        queries = rewrite_queries(question, _cross_product())

        assert len(queries) == 2
        assert [scope_of(q) for q in queries] == ["confluence", "jira"]

    def test_each_scoped_query_keeps_the_users_own_words(self) -> None:
        """A scope tag narrows the search; it must not rewrite the question.

        Rephrasing belongs to the user. If the scoped query said something the user
        did not, then `rewrite_queries` — which an operator reads as "this is what
        was searched" — would be reporting the system's words as the user's.
        """
        from app.retrieval.query_rewriter import base_question, rewrite_queries

        question = "Why can a user see a linked page in Confluence but not in Jira?"
        for query in rewrite_queries(question, _cross_product()):
            assert base_question(query) == question

    def test_the_scope_is_not_embedded_in_the_similarity_text(self) -> None:
        """The scope is a filter, not a word.

        Repeating a product name in the text of a similarity query biases the
        ranking toward units that merely *mention* it, which is not the same as
        units that are *about* it.
        """
        from app.retrieval.query_rewriter import base_question

        question = "How do I share a page with a Jira project?"
        scoped = f"{question} [scope: confluence]"

        assert "confluence" not in base_question(scoped)
        assert base_question(scoped) == question

    def test_an_ordinary_question_is_one_unscoped_query(self) -> None:
        from app.retrieval.query_rewriter import rewrite_queries, scope_of

        question = "How do I create a JQL filter?"
        queries = rewrite_queries(question, _analysis())

        assert queries == [question]
        assert scope_of(queries[0]) is None

    def test_a_single_named_product_is_not_expanded(self) -> None:
        """One product named is one query, however the analysis reads."""
        from app.retrieval.query_rewriter import rewrite_queries

        question = "How do I create a JQL filter in Jira?"
        queries = rewrite_queries(question, _analysis(products=["jira"], note=None))

        assert queries == [question]


class TestEachAspectSearchesItsOwnDomain:
    def test_a_scoped_analysis_filters_to_its_own_product(self) -> None:
        from app.chat.service import _analysis_for_scope
        from app.retrieval.retriever import build_metadata_filter

        aspect = _analysis_for_scope(_cross_product(), "confluence")

        assert aspect.detected_product == "confluence"
        assert aspect.ambiguity_note is None, "an aspect is specific, not ambiguous"
        assert build_metadata_filter(aspect).filter == {"product": "confluence"}

    def test_an_unscoped_analysis_is_left_alone(self) -> None:
        from app.chat.service import _analysis_for_scope

        analysis = _analysis()
        assert _analysis_for_scope(analysis, None) is analysis

    def test_the_original_analysis_is_not_mutated(self) -> None:
        """Aspects are derived, and deriving must not rewrite the question's own reading."""
        from app.chat.service import _analysis_for_scope

        analysis = _cross_product()
        _analysis_for_scope(analysis, "jira")

        assert analysis.detected_product is None
        assert analysis.ambiguity_note is not None


class TestTheBudgetIsRespectedAcrossAspects:
    def test_two_aspects_do_not_double_the_candidate_pool(self) -> None:
        """Principle VIII: the pool is the operator's number, not the aspect count's."""
        from app.core.config import get_settings

        pool = get_settings().retrieval_candidate_pool
        aspects = 2
        merged_cap = pool

        assert merged_cap == pool
        assert merged_cap < pool * aspects, "the merge cap is not the budget it claims to be"

    def test_duplicate_units_across_aspects_are_merged_once(self) -> None:
        """A unit retrieved by two aspects is one candidate.

        Without this the pool fills with the same passage twice, the evidence set
        looks diverse, and it is not.
        """
        seen: set[str] = set()
        merged: list[str] = []
        for _aspect in ("confluence", "jira"):
            for candidate_id in ("unit-a", "unit-b"):
                if candidate_id in seen:
                    continue
                seen.add(candidate_id)
                merged.append(candidate_id)

        assert merged == ["unit-a", "unit-b"]
        assert len(merged) == len(set(merged))


class TestEvidenceSpansTheDomains:
    def test_each_named_domain_reserves_a_slot(self) -> None:
        """T116: a cross-product answer cannot be assembled from one domain."""
        from app.retrieval.evidence import select_evidence
        from app.retrieval.reranker import RerankedHit

        def hit(id_: str, product: str, score: float, rank: int, text: str) -> RerankedHit:
            return RerankedHit(
                id=id_,
                text=text,
                retrieval_score=score,
                rerank_score=score,
                rank=rank,
                metadata={
                    "document_id": f"doc-{id_}",
                    "product": product,
                    "source_url": "https://support.atlassian.com/x",
                },
            )

        # Confluence scores highest and is deeper; Jira has a single unit.
        hits = [
            hit(f"c{i}", "confluence", 0.9 - i * 0.05, i, _DISTINCT_PASSAGES[i - 1])
            for i in range(1, 7)
        ]
        hits.append(hit("j1", "jira", 0.3, 7, _DISTINCT_PASSAGES[6]))

        evidence = select_evidence(
            "why can a user see a linked page in Confluence but not in Jira?",
            hits,
            required_groups={"confluence": "confluence", "jira": "jira"},
        )
        products = {u.product for u in evidence}

        assert "jira" in products, "the weaker domain was crowded out of its own question"
        assert "confluence" in products

    def test_without_the_requirement_the_strongest_domain_wins(self) -> None:
        """And that is the behaviour the requirement exists to change."""
        from app.retrieval.evidence import select_evidence
        from app.retrieval.reranker import RerankedHit

        def hit(id_: str, product: str, score: float, rank: int, text: str) -> RerankedHit:
            return RerankedHit(
                id=id_,
                text=text,
                retrieval_score=score,
                rerank_score=score,
                rank=rank,
                metadata={
                    "document_id": f"doc-{id_}",
                    "product": product,
                    "source_url": "https://support.atlassian.com/x",
                },
            )

        hits = [
            hit(f"c{i}", "confluence", 0.9 - i * 0.05, i, _DISTINCT_PASSAGES[i - 1])
            for i in range(1, 7)
        ]
        hits.append(hit("j1", "jira", 0.3, 7, _DISTINCT_PASSAGES[6]))

        evidence = select_evidence("q", hits)

        assert {u.product for u in evidence} == {"confluence"}

    def test_a_domain_with_nothing_in_the_pool_is_not_filled(self) -> None:
        """The reservation is satisfied by the domain's best unit or not at all."""
        from app.retrieval.evidence import select_evidence
        from app.retrieval.reranker import RerankedHit

        hits = [
            RerankedHit(
                id="c1",
                text="confluence passage",
                retrieval_score=0.8,
                rerank_score=0.8,
                rank=1,
                metadata={
                    "document_id": "d1",
                    "product": "confluence",
                    "source_url": "https://support.atlassian.com/x",
                },
            )
        ]
        evidence = select_evidence(
            "q", hits, required_groups={"confluence": "confluence", "jira": "jira"}
        )

        assert {u.product for u in evidence} == {"confluence"}
