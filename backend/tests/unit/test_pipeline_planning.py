"""The pipeline's decisions, tested without a database or a network.

T058, T060. The two decisions that are worth pinning on their own are the ones
whose failure is invisible in production: what the vector store is asked to
write, and what it is asked to remove. A supersession plan that deleted one
vector too many does not raise — it silently un-answers a question.

SC-012 (repeated crawls never accumulate duplicates), FR-026, R-003, R-005.
"""

from __future__ import annotations

import uuid

import pytest
from app.db.models import DocumentUnit
from app.ingestion.chunker import Chunk
from app.ingestion.fingerprint import unit_text_fingerprint
from app.ingestion.metadata import PageMetadata
from app.ingestion.pipeline import plan_supersession, unit_metadata, vector_id_for

pytestmark = pytest.mark.unit

DOC = uuid.UUID("11111111-2222-3333-4444-555555555555")
SOURCE = uuid.UUID("66666666-7777-8888-9999-000000000000")


def chunk(ordinal: int, text: str) -> Chunk:
    return Chunk(
        ordinal=ordinal, text=text, heading_path=["Docs", f"H{ordinal}"], block_types=["p"]
    )


def stored(ordinal: int, text: str, document_id: uuid.UUID = DOC) -> DocumentUnit:
    return DocumentUnit(
        document_id=document_id,
        ordinal=ordinal,
        vector_id=vector_id_for(document_id, ordinal),
        text_fingerprint=unit_text_fingerprint(text),
        heading_path=["Docs"],
        block_types=["p"],
        token_count=len(text.split()),
        overlap_tokens=0,
    )


class TestVectorIds:
    def test_scheme_is_document_then_four_digit_ordinal(self) -> None:
        assert vector_id_for(DOC, 7) == f"{DOC}#0007"
        assert vector_id_for(DOC, 1234) == f"{DOC}#1234"

    def test_same_ordinal_same_id_on_every_crawl(self) -> None:
        # R-005's immutability is what lets a citation minted before a re-crawl
        # keep resolving; if this changed, supersession would orphan vectors.
        assert vector_id_for(DOC, 3) == vector_id_for(DOC, 3)


class TestUnitMetadata:
    def _metadata(self) -> dict[str, object]:
        from datetime import UTC, datetime

        return unit_metadata(
            document_id=DOC,
            source_id=SOURCE,
            url="https://support.atlassian.com/jira-software-cloud/docs/x/",
            metadata=PageMetadata(
                title="Title",
                title_confidence=0.95,
                product="jira",
                product_confidence=0.95,
                category="user-documentation",
                category_confidence=0.6,
                page_type="how_to",
                page_type_confidence=0.6,
                language="en",
                language_confidence=0.95,
                canonical_url="https://support.atlassian.com/jira-software-cloud/docs/x/",
            ),
            chunk=chunk(0, "text"),
            content_fingerprint="f" * 64,
            indexed_at=datetime(2026, 10, 3, tzinfo=UTC),
        )

    def test_filter_fields_present(self) -> None:
        metadata = self._metadata()
        for field in ("document_id", "source_id", "product", "category", "page_type", "language"):
            assert field in metadata, f"{field} is needed to filter (FR-014)"

    def test_registry_join_fields_present(self) -> None:
        metadata = self._metadata()
        assert metadata["document_id"] == str(DOC)
        assert metadata["ordinal"] == 0
        assert metadata["content_fingerprint"] == "f" * 64
        assert metadata["source_url"].startswith("https://")

    def test_namespace_is_absent(self) -> None:
        # Reserved by the store; writing it in metadata fails the write.
        assert "namespace" not in self._metadata()

    def test_no_value_is_a_nested_mapping(self) -> None:
        # Measured: Pinecone has no nested-object metadata, and a nested dict
        # silently fails to match — a filter that looks configured and is not.
        for key, value in self._metadata().items():
            assert not isinstance(value, dict), f"{key} is a nested mapping"

    def test_no_dimension_anywhere(self) -> None:
        # R-003 / Principle VII: the store embeds, so a dimension literal here
        # would be a hard-coded belief about a model nobody re-checks.
        rendered = " ".join(str(v) for v in self._metadata().values())
        assert "dimension" not in rendered
        assert "1024" not in rendered


class TestSupersessionPlan:
    def test_first_crawl_writes_everything_and_orphans_nothing(self) -> None:
        plan = plan_supersession({}, [chunk(0, "a"), chunk(1, "b")])
        assert [c.ordinal for c in plan.to_write] == [0, 1]
        assert plan.unchanged == []
        assert plan.orphaned_vector_ids == []

    def test_identical_recrawl_writes_nothing(self) -> None:
        # SC-012: an unchanged page must not re-embed a token.
        existing = {0: stored(0, "a"), 1: stored(1, "b")}
        plan = plan_supersession(existing, [chunk(0, "a"), chunk(1, "b")])
        assert plan.to_write == []
        assert plan.unchanged == [0, 1]
        assert plan.orphaned_vector_ids == []
        assert plan.writes_needed is False

    def test_unchanged_text_at_a_different_ordinal_is_not_unchanged(self) -> None:
        # Ordinal is part of identity. Two identical paragraphs at ordinals 0
        # and 1 are two units, and moving one is a change.
        existing = {0: stored(0, "a"), 1: stored(1, "b")}
        plan = plan_supersession(existing, [chunk(0, "a"), chunk(1, "a")])
        assert [c.ordinal for c in plan.to_write] == [1]

    def test_changed_unit_is_written_and_supersedes_in_place(self) -> None:
        existing = {0: stored(0, "a")}
        plan = plan_supersession(existing, [chunk(0, "a changed")])
        assert [c.ordinal for c in plan.to_write] == [0]
        # Superseded is recorded, but the id is the same one: the write
        # overwrites the predecessor rather than replacing the reference.
        assert plan.superseded_vector_ids == [vector_id_for(DOC, 0)]
        assert plan.orphaned_vector_ids == []

    def test_shrinking_page_orphans_only_the_tail(self) -> None:
        existing = {0: stored(0, "a"), 1: stored(1, "b"), 2: stored(2, "c")}
        plan = plan_supersession(existing, [chunk(0, "a")])
        assert [c.ordinal for c in plan.to_write] == []
        assert plan.orphaned_vector_ids == [vector_id_for(DOC, 1), vector_id_for(DOC, 2)]

    def test_growth_writes_only_the_new_tail(self) -> None:
        existing = {0: stored(0, "a")}
        plan = plan_supersession(existing, [chunk(0, "a"), chunk(1, "b"), chunk(2, "c")])
        assert [c.ordinal for c in plan.to_write] == [1, 2]
        assert plan.unchanged == [0]
        assert plan.orphaned_vector_ids == []

    def test_mixed_change_reports_each_verdict(self) -> None:
        existing = {0: stored(0, "a"), 1: stored(1, "b"), 2: stored(2, "c")}
        plan = plan_supersession(existing, [chunk(0, "a"), chunk(1, "b changed"), chunk(2, "c")])
        assert plan.unchanged == [0, 2]
        assert [c.ordinal for c in plan.to_write] == [1]

    def test_repeated_recrawls_are_idempotent(self) -> None:
        existing = {0: stored(0, "a"), 1: stored(1, "b")}
        first = plan_supersession(existing, [chunk(0, "a"), chunk(1, "b")])
        second = plan_supersession(existing, [chunk(0, "a"), chunk(1, "b")])
        assert first.to_write == second.to_write == []
        assert first.orphaned_vector_ids == second.orphaned_vector_ids == []
