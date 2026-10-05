"""Domain discipline and evidence diversity (T112, T113 / FR-021, FR-022).

Two properties that pull against each other, and both have to hold:

**T112 — stay inside the domain that answered.** When one product fully answers
the question, the answer comes from that product. Padding it with a loosely
related page from another domain makes the answer look thorough and is really a
second, weaker answer nobody asked for.

**T113 — do not let one page crowd out a distinct source.** Near-duplicate
passages from a single page are the same fact twice, and six of them look like
six sources while being one. The source count is reported rather than assumed, so
"three sources" cannot be printed over evidence that came from one page.

These overlap deliberately: T112 is about *which domains* and T113 about *which
pages within them*. Both are asserted on the same selector, because both are ways
the same evidence set can overstate itself.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


def _hit(
    id_: str,
    *,
    rank: int,
    rerank: float,
    document_id: str,
    product: str,
    text: str,
    url: str = "https://support.atlassian.com/x",
):
    from app.retrieval.reranker import RerankedHit

    return RerankedHit(
        id=id_,
        text=text,
        retrieval_score=0.5,
        rerank_score=rerank,
        rank=rank,
        metadata={"document_id": document_id, "product": product, "source_url": url, "title": url},
    )


#: Genuinely different sentences. Near-duplicate detection works on text, so a
#: fixture of reworded versions of one sentence measures folding rather than
#: anything about sources.
_PASSAGES = [
    "Boards show the work in a team has agreed to complete.",
    "A sprint is a time-boxed iteration with a committed set of issues.",
    "Permissions on a board decide who may view it and who may edit it.",
    "A filter saved from a board keeps its JQL criteria until edited.",
    "Epics group issues into a larger body of work for planning.",
    "The backlog is ordered by the team and is not bound to a sprint.",
    "Automation rules act on issues when a filter matches them.",
    "Linked issues show the relationship in both directions.",
    "A quick filter narrows the board to one assignee for review.",
    "Issue types are configurable and each may have its own screen.",
]


class TestDomainDiscipline:
    def test_an_answer_from_one_domain_stays_in_that_domain(self) -> None:
        """US4 scenario 3: one product fully answers, so one product is cited."""
        from app.retrieval.evidence import select_evidence

        hits = [
            _hit(
                f"j{i}",
                rank=i,
                rerank=0.9 - i * 0.02,
                document_id=f"jdoc{i}",
                product="jira",
                text=_PASSAGES[i - 1],
            )
            for i in range(1, 7)
        ]
        # A Confluence page about boards, weaker, and not needed.
        hits.append(
            _hit(
                "c1",
                rank=7,
                rerank=0.31,
                document_id="cdoc1",
                product="confluence",
                text="A board in Confluence lists content rather than work items.",
            )
        )

        evidence = select_evidence("how does a Jira board work", hits, prefer_group="jira")

        assert evidence, "the answerable domain produced nothing"
        assert {u.product for u in evidence} == {"jira"}

    def test_the_unneeded_domain_is_dropped_even_when_it_is_related(self) -> None:
        """Related is not the test; *needed* is.

        A Confluence page that genuinely discusses boards is still not evidence for
        a Jira question when four Jira pages already answered it.
        """
        from app.retrieval.evidence import select_evidence

        hits = [
            _hit(
                f"j{i}",
                rank=i,
                rerank=0.8 - i * 0.02,
                document_id=f"jdoc{i}",
                product="jira",
                text=_PASSAGES[i - 1],
            )
            for i in range(1, 6)
        ]
        hits.append(
            _hit(
                "c1",
                rank=6,
                rerank=0.6,
                document_id="cdoc1",
                product="confluence",
                text=_PASSAGES[9],
            )
        )

        evidence = select_evidence("how does a Jira board work", hits, prefer_group="jira")

        assert "confluence" not in {u.product for u in evidence}


class TestEvidenceDiversity:
    def test_near_duplicate_passages_from_one_page_fold(self) -> None:
        """FR-022: three units from one page carrying the same fact are one fact."""
        from app.retrieval.evidence import select_evidence

        same = "Boards show the work a team has agreed to complete, in the order they agreed."
        hits = [
            _hit("a", rank=1, rerank=0.9, document_id="doc-1", product="jira", text=same),
            _hit("b", rank=2, rerank=0.88, document_id="doc-1", product="jira", text=same),
            _hit("c", rank=3, rerank=0.86, document_id="doc-1", product="jira", text=same),
        ]

        evidence = select_evidence("how do boards work", hits)

        assert len(evidence) == 1
        assert evidence[0].hit.id == "a"

    def test_a_distinct_source_is_not_crowded_out_by_repeats_of_another(self) -> None:
        """The property T113 exists for: diversity over repetition."""
        from app.retrieval.evidence import select_evidence

        same = "Boards show the work a team has agreed to complete, in the order they agreed."
        hits = [
            _hit(
                f"a{i}",
                rank=i,
                rerank=0.9 - i * 0.01,
                document_id="doc-1",
                product="jira",
                text=same,
            )
            for i in range(1, 7)
        ]
        hits.append(
            _hit(
                "z",
                rank=7,
                rerank=0.42,
                document_id="doc-2",
                product="jira",
                text="A filter saved from a board keeps its JQL criteria until it is edited.",
            )
        )

        evidence = select_evidence("how do boards work", hits, max_units=4)

        documents = {u.document_id for u in evidence}
        assert len(documents) >= 2, "six repeats of one page passed as a diverse evidence set"
        assert "doc-2" in documents

    def test_the_source_count_is_reported_from_the_documents_not_the_units(self) -> None:
        """Two units from one page is one source, whatever the unit count says."""
        from app.retrieval.evidence import select_evidence

        hits = [
            _hit("a", rank=1, rerank=0.9, document_id="doc-1", product="jira", text=_PASSAGES[0]),
            _hit("b", rank=2, rerank=0.88, document_id="doc-1", product="jira", text=_PASSAGES[1]),
            _hit(
                "c",
                rank=3,
                rerank=0.86,
                document_id="doc-2",
                product="jira",
                text=_PASSAGES[2],
                url="https://support.atlassian.com/y",
            ),
        ]

        evidence = select_evidence("q", hits)
        sources = {u.document_id for u in evidence}

        assert len(evidence) == 3
        assert len(sources) == 2, "three units were reported as three sources"

    def test_a_third_unit_from_one_document_is_excluded(self) -> None:
        """Two units from a page can both be needed; a third is repetition."""
        from app.retrieval.evidence import select_evidence

        hits = [
            _hit("a", rank=1, rerank=0.9, document_id="doc-1", product="jira", text=_PASSAGES[0]),
            _hit("b", rank=2, rerank=0.88, document_id="doc-1", product="jira", text=_PASSAGES[1]),
            _hit("c", rank=3, rerank=0.86, document_id="doc-1", product="jira", text=_PASSAGES[2]),
            _hit(
                "d",
                rank=4,
                rerank=0.84,
                document_id="doc-2",
                product="jira",
                text=_PASSAGES[3],
                url="https://support.atlassian.com/y",
            ),
        ]

        evidence = select_evidence("q", hits)
        from_one_page = [u for u in evidence if u.document_id == "doc-1"]

        assert len(from_one_page) == 2
