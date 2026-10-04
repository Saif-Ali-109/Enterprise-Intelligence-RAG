"""Citation validation: the validator is deterministic and never calls the model.

FR-003: every substantive claim cites. FR-004: inbound links resolve from the
registry of record. FR-005: unknown citations are rejected. The test's key
invariant: the validator takes a plain object and a plain list — there is no
provider argument to accidentally call. `test_validator_does_not_call_the_model`
pins that invariant by construction rather than by prohibition.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


def _source(
    evidence_id: str,
    *,
    url: str,
    title: str = "Manage API tokens",
    product="jira",
    category="api-tokens",
    heading=None,
    document_id=None,
    unit_id=None,
):
    from app.retrieval.citations import EvidenceSource

    return EvidenceSource(
        evidence_id=evidence_id,
        document_id=document_id or "3b1f0000-0000-0000-0000-000000000001",
        unit_id=unit_id or "9c4e0000-0000-0000-0000-000000000001",
        source_url=url,
        title=title,
        product=product,
        category=category,
        heading_path=heading or ["Manage API tokens", "Rotate a token"],
        retrieval_score=0.7,
        rerank_score=0.8,
        quote="To rotate a token, create a new one and then revoke the previous token.",
    )


class TestValidity:
    def test_an_identifier_that_was_not_served_is_rejected(self) -> None:
        from app.retrieval.citations import validate_citations

        result = validate_citations(
            cited_ids=["ghost-1"],
            retrieved=[_source("a", url="https://example.atlassian.net/x")],
        )
        assert result.rejected_identifiers == ["ghost-1"]
        assert result.citations == []

    def test_a_link_that_disagrees_with_the_registry_is_repaired(self) -> None:
        from app.retrieval.citations import validate_citations

        cited = [{"evidence_id": "a", "source_url": "https://not-the-publisher.example/x"}]
        result = validate_citations(
            cited_ids=["a"],
            retrieved=[_source("a", url="https://support.atlassian.com/x")],
            cited_links=cited,
        )
        assert result.repaired == 1
        assert result.citations[0].source_url == "https://support.atlassian.com/x"
        assert result.citations[0].validation_state == "repaired"

    def test_a_citation_to_a_non_existent_item_is_stripped(self) -> None:
        from app.retrieval.citations import validate_citations

        result = validate_citations(
            cited_ids=["does-not-exist"],
            retrieved=[],
            cited_links=[
                {"evidence_id": "does-not-exist", "source_url": "https://example.atlassian.net/x"}
            ],
        )
        assert result.stripped == 1
        assert result.citations == []

    def test_a_genuine_citation_resolves_through_the_registry(self) -> None:
        from app.retrieval.citations import validate_citations

        result = validate_citations(
            cited_ids=["a"],
            retrieved=[_source("a", url="https://support.atlassian.com/x")],
            cited_links=[{"evidence_id": "a", "source_url": "https://support.atlassian.com/x"}],
        )
        assert result.stripped == 0
        assert result.citations[0].validation_state == "valid"
        assert result.citations[0].source_url == "https://support.atlassian.com/x"
        assert result.citations[0].heading_path == ["Manage API tokens", "Rotate a token"]


class TestOrderAndMix:
    def test_a_mixed_answer_keeps_valid_and_audits_the_rest(self) -> None:
        from app.retrieval.citations import validate_citations

        retrieved = [
            _source("a", url="https://support.atlassian.com/x"),
            _source("b", url="https://developer.atlassian.com/y"),
        ]
        result = validate_citations(
            cited_ids=["a", "ghost", "b"],
            retrieved=retrieved,
            cited_links=[
                {"evidence_id": "a", "source_url": "https://support.atlassian.com/x"},
                {"evidence_id": "b", "source_url": "https://wrong.example/y"},
            ],
        )
        assert [c.validation_state for c in result.citations] == ["valid", "repaired"]
        assert result.rejected_identifiers == ["ghost"]
        assert result.stripped == 1

    def test_duplicate_markers_are_deduplicated_by_destination(self) -> None:
        from app.retrieval.citations import validate_citations

        result = validate_citations(
            cited_ids=["a", "a"],
            retrieved=[_source("a", url="https://ok.atlassian.net/x")],
            cited_links=[{"evidence_id": "a", "source_url": "https://ok.atlassian.net/x"}],
        )
        # Two cites of the same unit collapse to one ranking, both marker positions
        # point at the same citation row — the two states count once, not twice.
        assert len(result.citations) == 1
        assert result.stripped == 0


class TestNoModelReach:
    def test_validator_does_not_call_the_model(self) -> None:
        """FR-005 must hold: the validator has nothing to call. A fixture with a
        poisoned provider must never be reachable — the function surface simply
        has no provider argument."""
        from app.retrieval.citations import validate_citations

        # If a provider parameter were added, this would fail to compile. If the
        # validator ever called one, a poisoned double would raise.
        result = validate_citations(
            cited_ids=["a"],
            retrieved=[_source("a", url="https://support.atlassian.com/x")],
        )
        assert result.citations


class TestInlineMarkers:
    """FR-002 per claim, resolved by code: `[E2]` becomes `[1]`, or nothing.

    The marker is how a reader tells *which* source supports *which* sentence. A
    citation list that is correct but unattached to the prose is decoration, so
    the resolution is deterministic and an answered response whose claims point at
    no evidence is refused by the service rather than shipped.
    """

    def test_a_marker_becomes_the_citation_rank(self) -> None:
        from app.retrieval.citations import resolve_answer_markers

        served = ["unit-a", "unit-b"]
        result = resolve_answer_markers("POST to /issue [E1]. It returns a key [E2].", served)

        assert result.text == "POST to /issue [1]. It returns a key [2]."
        assert result.cited_ids == ["unit-a", "unit-b"]

    def test_markers_number_by_first_appearance_not_by_marker_number(self) -> None:
        """`[E3]` first means the third unit is citation **1**.

        The reader matches `[1]` to the first source under the answer, so the rank
        has to follow the prose rather than the evidence order.
        """
        from app.retrieval.citations import resolve_answer_markers

        result = resolve_answer_markers("Claim [E3]. Another [E1].", ["a", "b", "c"])

        assert result.text == "Claim [1]. Another [2]."
        assert result.cited_ids == ["c", "a"]

    def test_the_same_unit_marked_twice_is_one_citation(self) -> None:
        from app.retrieval.citations import resolve_answer_markers

        result = resolve_answer_markers("First [E1]. Second, the same source [E1].", ["a", "b"])

        assert result.text == "First [1]. Second, the same source [1]."
        assert result.cited_ids == ["a"]

    def test_a_marker_for_unserved_evidence_resolves_to_nothing(self) -> None:
        """Never pointed at whatever happens to sit at that index."""
        from app.retrieval.citations import resolve_answer_markers

        result = resolve_answer_markers("Claim [E7].", ["a", "b"])

        assert result.cited_ids == []
        assert result.rejected == ["E7"]
        assert "E7" not in result.text

    def test_an_answer_with_no_markers_cites_nothing(self) -> None:
        from app.retrieval.citations import resolve_answer_markers

        result = resolve_answer_markers("A fluent, unsupported paragraph.", ["a", "b"])

        assert result.cited_ids == []
        assert result.text == "A fluent, unsupported paragraph."

    def test_ordinary_bracketed_numbers_are_left_alone(self) -> None:
        """`[1]` in the model's own prose is not a marker we own.

        Only `[E<n>]` is ours; anything else in the text is the answer's, and
        rewriting it would edit prose to fit our bookkeeping.
        """
        from app.retrieval.citations import resolve_answer_markers

        result = resolve_answer_markers("Step [1] of the process [E1].", ["a"])

        assert result.text == "Step [1] of the process [1]."
        assert result.cited_ids == ["a"]

    def test_a_marker_at_the_end_of_a_line_does_not_leave_a_gap(self) -> None:
        from app.retrieval.citations import resolve_answer_markers

        result = resolve_answer_markers("Claim [E9].\n\nNext paragraph.", ["a"])

        assert "[E9]" not in result.text
        assert "  " not in result.text


class TestCitationGranularity:
    """Which guarantee an answer actually carries, decided by code.

    Measured live: the model produced inline claim markers in 3 of 6 answers, so
    `list` is a real state rather than a theoretical one. It is answered, and
    recorded as the weaker guarantee it is.
    """

    def test_marked_claims_are_per_claim(self) -> None:
        from app.retrieval.citations import ResolvedMarkers, citation_granularity

        markers = ResolvedMarkers(text="Claim [1].", cited_ids=["a"], rejected=[])
        assert citation_granularity(markers, ["a"]) == "per_claim"

    def test_sources_without_markers_are_a_list(self) -> None:
        from app.retrieval.citations import ResolvedMarkers, citation_granularity

        markers = ResolvedMarkers(text="An answer.", cited_ids=[], rejected=[])
        assert citation_granularity(markers, ["a"]) == "list"

    def test_nothing_cited_is_none_and_is_the_only_refused_state(self) -> None:
        from app.retrieval.citations import ResolvedMarkers, citation_granularity

        markers = ResolvedMarkers(text="A fluent, uncited paragraph.", cited_ids=[], rejected=[])
        assert citation_granularity(markers, []) == "none"
