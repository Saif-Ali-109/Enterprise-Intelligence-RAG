"""Refusals (T096 / FR-007, FR-008): the threshold gate and what a refusal says.

Four properties, and the first is the one that matters:

1. A candidate pool whose best unit is below `MIN_RERANK_SCORE`, or whose
   selected evidence falls below `MIN_EVIDENCE_SCORE`, produces a refusal. Not a
   hedged answer, not a shorter one.
2. The refusal names what was searched — queries, products, categories, filters,
   counts. A refusal you cannot act on is a dead end (FR-008).
3. Weakly related sources are offered as **leads**, each carrying
   `insufficient: true` as a literal constant.
4. A refusal with nothing even weakly related offers **nothing**. Inventing a
   "related document" to fill the space would be the same fabrication as an
   invented citation, wearing a smaller hat.

These are unit tests over the evidence gate and the refusal payload, driven with
doubles. The gates are pure decisions over scores; the payload is a dictionary.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


def _hit(
    id_: str,
    *,
    rank: int,
    rerank: float,
    retrieval: float = 0.5,
    document_id: str = "doc-1",
    metadata: dict | None = None,
):
    from app.retrieval.reranker import RerankedHit

    return RerankedHit(
        id=id_,
        text=f"passage {id_} about rotating an api token",
        retrieval_score=retrieval,
        rerank_score=rerank,
        rank=rank,
        metadata=metadata if metadata is not None else {"document_id": document_id},
    )


def _page(document_id: str, url: str = "https://support.atlassian.com/x") -> dict:
    """The metadata a live candidate carries: a page, a title, and a path."""
    return {
        "document_id": document_id,
        "source_url": url,
        "title": "Manage API tokens",
        "heading_path": ["Manage API tokens"],
    }


class _Settings:
    """The two gates, held as settings so the test reads like the operator's view."""

    def __init__(self, min_rerank_score: float | None, min_evidence_score: float | None) -> None:
        self.min_rerank_score = min_rerank_score
        self.min_evidence_score = min_evidence_score


class TestThresholdGate:
    def test_a_pool_below_the_rerank_floor_is_refused(self) -> None:
        from app.retrieval.evidence import gate_evidence

        hits = [_hit("a", rank=1, rerank=0.31), _hit("b", rank=2, rerank=0.12)]
        decision = gate_evidence(hits, thresholds=_Settings(0.5, None))

        assert decision.refused is True
        assert decision.reason == "BELOW_MIN_RERANK_SCORE"

    def test_a_pool_above_the_rerank_floor_is_not_refused(self) -> None:
        from app.retrieval.evidence import gate_evidence

        hits = [_hit("a", rank=1, rerank=0.83), _hit("b", rank=2, rerank=0.61)]
        assert gate_evidence(hits, thresholds=_Settings(0.5, None)).refused is False

    def test_an_evidence_set_below_the_evidence_floor_is_refused(self) -> None:
        from app.retrieval.evidence import gate_evidence

        hits = [_hit("a", rank=1, rerank=0.9)]
        decision = gate_evidence(hits, thresholds=_Settings(None, 0.95))

        assert decision.refused is True
        assert decision.reason == "BELOW_MIN_EVIDENCE_SCORE"

    def test_an_unset_gate_admits_everything(self) -> None:
        """`null` means "not configured", which is not the same as `0.0`.

        A default of zero would make the gate look configured while admitting
        everything that passed retrieval at all — the opposite of a threshold.
        """
        from app.retrieval.evidence import gate_evidence

        hits = [_hit("a", rank=1, rerank=0.01)]
        assert gate_evidence(hits, thresholds=_Settings(None, None)).refused is False

    def test_the_rerank_floor_reads_the_best_unit_not_the_average(self) -> None:
        """One strong unit is not an average of strong and weak.

        An average would let five irrelevant passages launder one relevant one
        into a passing score, and the answer would then be supported by the
        passage the average is made of — which is not the one that was relevant.
        """
        from app.retrieval.evidence import gate_evidence

        mixed = [_hit("a", rank=1, rerank=0.91)] + [
            _hit(f"w{i}", rank=i, rerank=0.02) for i in range(2, 7)
        ]
        assert gate_evidence(mixed, thresholds=_Settings(0.5, None)).refused is False

    def test_the_evidence_floor_reads_the_selected_set(self) -> None:
        """`MIN_EVIDENCE_SCORE` judges the set that would be answered from.

        It is the weakest unit in the *selected* set, not the pool's median: an
        answer built from three strong units and one useless one is an answer
        with a useless claim in it.
        """
        from app.retrieval.evidence import gate_evidence

        hits = [
            _hit("a", rank=1, rerank=0.9),
            _hit("b", rank=2, rerank=0.8),
            _hit("c", rank=3, rerank=0.2),
            _hit("d", rank=4, rerank=0.1),
        ]
        assert gate_evidence(hits, thresholds=_Settings(None, 0.5)).refused is True

    def test_the_gate_never_says_why_in_the_operators_words(self) -> None:
        """The reason is a code, not prose — the refusal renders it."""
        from app.retrieval.evidence import gate_evidence

        decision = gate_evidence([_hit("a", rank=1, rerank=0.1)], thresholds=_Settings(0.5, None))
        assert decision.reason in {"BELOW_MIN_RERANK_SCORE", "BELOW_MIN_EVIDENCE_SCORE"}
        assert isinstance(decision.best_score, float)


class TestLeads:
    def test_weakly_related_sources_become_leads(self) -> None:
        """FR-008: offered, clearly labelled, never as answers."""
        from app.retrieval.evidence import build_leads, gate_evidence

        hits = [
            _hit("a", rank=1, rerank=0.34, metadata=_page("doc-1")),
            _hit("b", rank=2, rerank=0.31, metadata=_page("doc-2")),
        ]
        decision = gate_evidence(hits, thresholds=_Settings(0.5, None))
        leads = build_leads(hits, limit=3)

        assert decision.refused is True
        assert len(leads) == 2
        assert all(lead["insufficient"] is True for lead in leads)
        assert {lead["relevance"] for lead in leads} <= {0.34, 0.31}

    def test_a_refusal_with_nothing_related_offers_nothing(self) -> None:
        from app.retrieval.evidence import build_leads

        assert build_leads([], limit=3) == []

    def test_a_hit_below_the_lead_floor_is_not_a_lead(self) -> None:
        """A lead is a weak *relation*, not any old result.

        The floor is a fraction of the gate that refused it: a unit at 0.02 when
        the floor was 0.5 is not "weakly related", it is noise, and listing it
        would put an unrelated page in front of an operator who has just been
        told nothing was found.
        """
        from app.retrieval.evidence import build_leads

        hits = [
            _hit("strong", rank=1, rerank=0.44, metadata=_page("doc-1")),
            _hit("noise", rank=2, rerank=0.01, metadata=_page("doc-2")),
        ]
        leads = build_leads(hits, limit=3, floor=0.1)

        # 0.44 clears the 0.10 floor the caller set, 0.01 does not, and the
        # caller's floor replaces the fraction default entirely.
        assert [lead["relevance"] for lead in leads] == [0.44]

    def test_leads_carry_the_publisher_page_and_its_heading_path(self) -> None:
        from app.retrieval.evidence import build_leads

        hits = [
            _hit(
                "a",
                rank=1,
                rerank=0.4,
                metadata={
                    "document_id": "doc-1",
                    "source_url": "https://support.atlassian.com/x",
                    "title": "Manage API tokens",
                    "heading_path": ["Manage API tokens", "Rotate a token"],
                },
            )
        ]
        leads = build_leads(hits, limit=3, floor=0.1)

        assert leads[0]["source_url"] == "https://support.atlassian.com/x"
        assert leads[0]["title"] == "Manage API tokens"
        assert leads[0]["heading_path"] == ["Manage API tokens", "Rotate a token"]

    def test_a_hit_with_no_page_is_not_offered(self) -> None:
        """No link, no lead: a lead the reader cannot check is noise with a URL shape."""
        from app.retrieval.evidence import build_leads

        hits = [_hit("a", rank=1, rerank=0.4, metadata={"document_id": "doc-1"})]
        assert build_leads(hits, limit=3, floor=0.1) == []


class TestRefusalPayload:
    def test_a_refusal_names_what_was_searched(self) -> None:
        """FR-008: queries, products, and the counts, every time."""
        from app.chat.service import _refused

        result = _refused(
            request_id="0f3c1d2a-6b4e-4c1f-9a77-2b8e5d0c4a11",
            question="How do I rotate an API key?",
            reason="INSUFFICIENT_EVIDENCE",
            searched={
                "queries": ["How do I rotate an API key?"],
                "products": ["jira"],
                "applied_filters": {"product": "jira"},
                "candidates_retrieved": 2,
                "candidates_reranked": 2,
                "evidence_selected": 0,
            },
            leads=[],
            total_ms=2180,
            trace=None,
            query_analysis=None,
            stages_ms={"retrieve_ms": 900},
        )

        payload = result.payload
        assert payload["outcome"] == "refused"
        assert payload["answer"] is None
        assert payload["refusal_reason"] == "INSUFFICIENT_EVIDENCE"
        assert payload["citations"] == []
        assert payload["searched"]["queries"] == ["How do I rotate an API key?"]
        assert payload["searched"]["products"] == ["jira"]
        assert payload["searched"]["applied_filters"] == {"product": "jira"}
        assert payload["searched"]["candidates_retrieved"] == 2

    def test_a_refusal_is_a_success_with_a_request_id(self) -> None:
        """It has to be traceable: the request id is what a log line carries."""
        from app.chat.service import _refused

        result = _refused(
            request_id="0f3c1d2a-6b4e-4c1f-9a77-2b8e5d0c4a11",
            question="q",
            reason="INSUFFICIENT_EVIDENCE",
            searched={
                "queries": ["q"],
                "products": [None],
                "applied_filters": {},
                "candidates_retrieved": 0,
                "candidates_reranked": 0,
                "evidence_selected": 0,
            },
            leads=[],
            total_ms=1,
            trace=None,
            query_analysis=None,
            stages_ms={},
        )

        assert result.payload["request_id"] == "0f3c1d2a-6b4e-4c1f-9a77-2b8e5d0c4a11"
        assert result.payload["timing"]["total_ms"] == 1
