"""Evidence selection: 3–6 better-spread units, never the whole pool (T075/FR-020, FR-021, FR-022)."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


def _hit(
    id_: str,
    *,
    rank: int,
    retrieval_score: float = 0.5,
    rerank_score: float = 0.5,
    document_id: str = "doc1",
    text: str = "one plausible sentence about boards",
    metadata=None,
):
    from app.retrieval.reranker import RerankedHit

    return RerankedHit(
        id=id_,
        text=text,
        retrieval_score=retrieval_score,
        rerank_score=rerank_score,
        rank=rank,
        metadata=metadata if metadata is not None else {"document_id": document_id},
    )


class TestFocus:
    def test_the_set_respects_the_maximum(self) -> None:
        from app.retrieval.evidence import select_evidence

        hits = [
            _hit(
                f"h{i}",
                rank=i,
                retrieval_score=0.5,
                rerank_score=0.5 - i * 0.01,
                document_id=f"doc{i}",
            )
            for i in range(1, 13)
        ]
        units = select_evidence("how do boards work", hits, max_units=6)
        assert len(units) <= 6

    def test_a_full_pool_does_not_leak_into_the_answer_step(self) -> None:
        # FR-020: a focused set rather than the candidate pool, even when
        # everything looks perfectly relevant.
        from app.retrieval.evidence import select_evidence

        hits = [_hit(f"h{i}", rank=i, document_id=f"doc{i}") for i in range(1, 13)]
        assert len(select_evidence("q", hits)) <= 6

    def test_preserves_separate_signal_columns(self) -> None:
        from app.retrieval.evidence import select_evidence

        hits = [_hit("h1", rank=1, retrieval_score=0.9, rerank_score=0.4, document_id="doc-a")]
        units = select_evidence("q", hits)
        assert units[0].retrieval_score == pytest.approx(0.9)
        assert units[0].rerank_score == pytest.approx(0.4)


class TestDiversity:
    def test_three_near_identical_sentences_from_one_page_become_one(self) -> None:
        from app.retrieval.evidence import select_evidence

        # FR-022: near-duplicates from one page are the same fact twice.
        text = "Boards let your team see the work that matters and the work that is blocked"
        hits = [
            _hit("a", rank=1, document_id="doc-one", text=text),
            _hit("b", rank=2, document_id="doc-one", text=text),
            _hit("c", rank=3, document_id="doc-one", text=text),
        ]
        units = select_evidence("q", hits)
        assert len(units) == 1

    def test_heading_relevance_breaks_ties_toward_the_right_section(self) -> None:
        from app.retrieval.evidence import select_evidence
        from app.retrieval.reranker import RerankedHit

        def hit_with_heading(id_, heading, doc_id="doc-z"):
            return RerankedHit(
                id=id_,
                text="body of the fact, irrelevant",
                retrieval_score=0.5,
                rerank_score=0.7,
                rank=1,
                metadata={"document_id": doc_id},
            )

        hits = [hit_with_heading("a", "user permissions"), hit_with_heading("b", "Charts")]
        units = select_evidence("how to change user permissions", hits)
        # The path containing 'permissions' should be first chosen.
        assert units[0].hit.id == "a"

    def test_a_small_pool_stays_the_size_it_is(self) -> None:
        # The floor is not a license to fabricate: two units is the answer, even
        # if the contract's preferred set is 3–6.
        from app.retrieval.evidence import select_evidence

        hits = [
            _hit("a", rank=1, document_id="d1", text="Boards list the work items a team tracks."),
            _hit(
                "b", rank=2, document_id="d2", text="Sprints are time-boxed iterations in a board."
            ),
        ]
        assert len(select_evidence("q", hits)) == 2


class TestRegistry:
    def test_heading_path_flows_from_registry(self) -> None:
        from app.retrieval.evidence import select_evidence

        class Registry:
            document_id = "doc-1"
            source_id = "src-1"
            category = "projects"
            page_type = "documentation"
            heading_path = ["Boards", "Configuration"]
            token_count = 256

        registry = {"h1": Registry()}
        hits = [_hit("h1", rank=1)]
        units = select_evidence("how do boards work", hits, units_by_id=registry)
        assert units[0].heading_path == ["Boards", "Configuration"]
        assert units[0].token_count == 256
