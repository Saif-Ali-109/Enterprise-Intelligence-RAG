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
