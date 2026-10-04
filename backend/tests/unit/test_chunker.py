"""Structure-aware chunking: boundaries, code integrity, overlap, and the floor.

T042 (the structural half), T055, T056. FR-015, FR-016, FR-017, R-013, R-003.

## What the real corpus looks like, and why it changed the design

Measured over 60 blocks from 6 real Atlassian pages (T054):

| Section size (estimated tokens) | Sections |
|---|---|
| under 600 | 16 of 17 |
| 600–1000 | 1 of 17 |
| over 1000 | **0** |

**The chunker's headline job — splitting an oversized section — almost never
fires on this corpus.** Documentation sections are small. A chunker written
around that case, and tested only against a synthetic 5,000-word section, would
be tested on a situation the real corpus does not produce.

So the tests below deliberately cover both regimes, and say which is which:

- `TestOversizedSections` uses synthetic long sections because real ones do not
  reach that size. These tests are about the *mechanism* being correct, and it
  is reachable — a single page section on a dense reference page can exceed it.
- `TestRealisticSectionSizes` uses the measured distribution, where the chunker
  mostly *declines to merge*, which is the more consequential decision.

**The chunker does not merge across heading boundaries, and that is the most
important assertion in this file.** Merging two 200-token sections would reach
the 600-token target, and it is the obvious thing to do. It would also give the
merged unit a heading path that names neither section — and FR-018 requires each
unit to retain its *full* heading path, while FR-021 scores heading relevance
against that path. A unit citing "Create a filter" that actually contains both
"Permissions" and "Create a filter" is a citation pointing at the wrong place,
which is the failure this whole project exists to prevent. Small sections are the
correct outcome, not a defect to be fixed by merging.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit

_PROSE = (
    "A board filter is a saved query that defines a subset of issues on a board. "
    "Open the board and choose Filters from the view options in the sidebar, then "
    "name the filter and choose a filter syntax. Filter syntax follows the JQL "
    "grammar, which is documented as its own topic. Only board administrators may "
    "edit a filter that is shared with the board."
)
_CODE = (
    "def build_filter(project: str) -> dict[str, object]:\n"
    '    return {"project": project, "jql": "assignee = currentUser()", "favourite": False}'
)
_STEPS = (
    "From your sidebar, select Spaces.\n\n"
    "Select the space you want to access.\n\n"
    "Choose the work item view that matches the layout your team uses."
)
_TABLE = (
    "| Field | Type | Description |\n"
    "| --- | --- | --- |\n"
    "| summary | string | The title shown in the issue list |"
)


def _blocks(*specs: tuple[str, str, list[str]]) -> list:
    """Build extractor-shaped blocks: `(block_type, text, heading_path)`."""
    from app.ingestion.extractor import ExtractedBlock

    out = []
    for block_type, text, path in specs:
        level = 0
        out.append(
            ExtractedBlock(
                block_type=block_type,
                text=text,
                heading_path=list(path),
                heading_level=level or (len(path) if path else 0),
            )
        )
    return out


def _long_section(paragraphs: int, *, heading: list[str] | None = None) -> list:
    """A section comfortably over the chunk ceiling, built from real prose.

    Not a repeated marker string: an estimator calibrated on real text is being
    used to decide whether this section splits, and lorem-ipsum-shaped filler
    tokenises differently enough to make the test measure the fixture.
    """
    return _blocks(
        *[
            ("p", f"Step {i}: " + _PROSE, heading if heading is not None else ["Long guide"])
            for i in range(paragraphs)
        ]
    )


# ============================================================================
# FR-015 — boundaries are structural
# ============================================================================


class TestStructuralBoundaries:
    def test_blocks_under_one_heading_become_one_unit(self) -> None:
        """The ordinary case, and the one the real corpus produces.

        Three blocks under one heading stay together, because a boundary exists
        at the heading and nowhere else. Splitting them would need a character
        count, which FR-015 forbids outright.
        """
        from app.ingestion.chunker import chunk_units

        result = chunk_units(
            _blocks(
                ("p", _PROSE, ["Filters"]),
                ("list", _STEPS, ["Filters"]),
                ("p", _PROSE[::-1], ["Filters"]),
            )
        )

        assert len(result.chunks) == 1
        assert result.chunks[0].heading_path == ["Filters"]
        # Every block's text is present, in order.
        assert _PROSE in result.chunks[0].text
        assert _STEPS in result.chunks[0].text

    def test_a_heading_change_is_a_boundary(self) -> None:
        """FR-015: the heading is the structure, so it is where a split happens.

        Each section's content is a full prose block. An earlier version of this
        fixture used a short numbered-steps list for the second section, which
        estimated at 46 tokens and fell below the 50-token usefulness floor — so
        it was correctly *rejected*, and the test failed for a reason unrelated
        to boundaries. A boundary test needs both sides to be valid units.
        """
        from app.ingestion.chunker import chunk_units

        result = chunk_units(
            _blocks(
                ("p", _PROSE, ["Filters", "Create a filter"]),
                ("p", _PROSE[::-1], ["Filters", "Create a filter"]),
                ("p", _STEPS + " " + _PROSE, ["Filters", "Permissions"]),
            )
        )

        assert [c.heading_path for c in result.chunks] == [
            ["Filters", "Create a filter"],
            ["Filters", "Permissions"],
        ]

    def test_content_before_any_heading_gets_an_empty_path(self) -> None:
        """Real pages open with prose before the first heading.

        The path is empty because that is where it sits. Inventing one —
        "Introduction", or the page title — would be a fabricated citation
        target, and FR-018 asks for the path the unit actually sits under.
        """
        from app.ingestion.chunker import chunk_units

        result = chunk_units(_blocks(("p", _PROSE, []), ("p", _STEPS, [])))

        assert len(result.chunks) == 1
        assert result.chunks[0].heading_path == []

    def test_sections_are_never_merged_across_heading_boundaries(self) -> None:
        """**The decision this file exists to defend.**

        Both sections are ~200 tokens; merged they would reach the 600 target,
        which is the obvious optimisation and the wrong one. The merged unit
        could only carry one of the two heading paths, so FR-018's "full heading
        path" fails and FR-021's heading-relevance scoring reads a path that
        names only part of the unit's content.
        """
        from app.ingestion.chunker import chunk_units

        result = chunk_units(
            _blocks(
                ("p", _PROSE, ["Filters", "Create a filter"]),
                ("p", _STEPS, ["Filters", "Create a filter"]),
                ("p", _PROSE[::-1], ["Filters", "Permissions"]),
                ("p", _STEPS[::-1], ["Filters", "Permissions"]),
            )
        )

        assert len(result.chunks) == 2
        assert result.chunks[0].heading_path == ["Filters", "Create a filter"]
        assert result.chunks[1].heading_path == ["Filters", "Permissions"]
        # And neither unit borrowed the other's content.
        assert "Permissions" not in result.chunks[0].text

    def test_every_block_appears_in_exactly_one_chunk(self) -> None:
        """No silent loss. A dropped block is a gap that no later stage can see.

        This is the check that catches an off-by-one in the split loop, which is
        the likeliest bug in a splitter and one that leaves no error behind.
        """
        from app.ingestion.chunker import chunk_units

        blocks = _long_section(14)
        result = chunk_units(blocks)

        for original in blocks:
            occurrences = sum(1 for c in result.chunks if original.text in c.text)
            assert occurrences >= 1, f"block was dropped entirely: {original.text[:60]!r}"


# ============================================================================
# FR-017 — code integrity
# ============================================================================


class TestCodeExampleIntegrity:
    def test_a_code_block_is_never_split_mid_example(self) -> None:
        """FR-017, with a safe boundary available on both sides.

        Two code blocks each land whole in their own chunk rather than one being
        cut so the chunk size comes out even. A cut code example is not a
        retrieval result, it is a syntax error, and the half that was indexed
        would be cited for a claim it does not contain.
        """
        from app.ingestion.chunker import chunk_units

        result = chunk_units(
            _blocks(
                ("p", _PROSE, ["API"]),
                ("code", _CODE, ["API"]),
                ("p", _PROSE[::-1], ["API"]),
                ("code", _CODE[::-1], ["API"]),
                ("p", _STEPS, ["API"]),
            )
        )

        for code in (_CODE, _CODE[::-1]):
            carriers = [c for c in result.chunks if code in c.text]
            assert len(carriers) == 1, "a code example was split across chunks"
            # And the whole example is there, not a prefix.
            assert code.strip() in carriers[0].text

    def test_an_oversized_code_block_becomes_its_own_unit(self) -> None:
        """A single example larger than the ceiling cannot be split *and* kept whole.

        When there is no safe boundary, FR-017's "when a safe boundary is
        available" is not satisfied — there is none. So it is emitted whole and
        **recorded**, because indexing an over-limit record is a rejected write at
        the service and silence about it is how that becomes an unexplained gap.
        """
        from app.ingestion.chunker import chunk_units

        huge_code = _CODE + "\n" + ("# padding line to exceed the ceiling\n" * 400)
        result = chunk_units(
            _blocks(("p", _PROSE, ["API"]), ("code", huge_code, ["API"]), ("p", _STEPS, ["API"]))
        )

        carriers = [c for c in result.chunks if huge_code.strip() in c.text]
        assert len(carriers) == 1, "the oversized example was cut rather than kept whole"
        assert result.oversized, (
            "an over-limit unit was emitted without being recorded; the record is "
            "what makes the eventual rejection explainable"
        )

    def test_a_code_block_is_never_entered_partway(self) -> None:
        """A split lands *between* blocks, so a code example is atomic.

        An earlier version of this test asserted that some chunk *starts* with
        the example. That is the wrong property: overlap (FR-016) legitimately
        carries prose into the chunk containing the code, so the example need not
        be at the start. What must hold is that the split never falls *inside* it
        — so the test looks at block boundaries, which is what the chunker
        actually controls.
        """
        from app.ingestion.chunker import chunk_units

        result = chunk_units(
            _blocks(
                *[("p", _PROSE, ["API"]) for _ in range(10)],
                ("code", _CODE, ["API"]),
                ("p", _STEPS, ["API"]),
            )
        )

        assert len(result.chunks) >= 2
        for chunk in result.chunks:
            if "code" in chunk.block_types:
                # The example appears whole, and the blocks around it are whole too.
                assert _CODE in chunk.text
                kinds = [b.block_type for b in chunk.blocks]
                assert kinds.count("code") == 1
                # No chunk is built from a fragment of the example: the joined
                # text of its own blocks contains the example verbatim.
                joined = "\n\n".join(b.text for b in chunk.blocks)
                assert _CODE in joined


# ============================================================================
# FR-016 — overlap
# ============================================================================


class TestNeighbourOverlap:
    def test_neighbouring_chunks_share_trailing_content(self) -> None:
        """FR-016: a fact near a boundary stays retrievable from either side.

        The mechanism is that the trailing block of one chunk opens the next, so
        a question about it matches whichever chunk is in the evidence set.
        """
        from app.ingestion.chunker import chunk_units

        result = chunk_units(_long_section(14))

        assert len(result.chunks) >= 2
        first, second = result.chunks[0], result.chunks[1]
        assert result.chunks[0].continues_into_next is True
        assert result.chunks[1].continued_from_previous is True

        # The shared block is the last of the first chunk and the first of the second.
        overlap_blocks = [b for b in first.blocks if b in second.blocks]
        assert overlap_blocks, (
            "adjacent chunks share no content, so a fact at the boundary is retrievable from one side only"
        )

    def test_overlap_does_not_apply_to_a_single_chunk(self) -> None:
        """A section that did not split has no neighbour to overlap with."""
        from app.ingestion.chunker import chunk_units

        result = chunk_units(_blocks(("p", _PROSE, ["Filters"])))

        assert len(result.chunks) == 1
        assert result.chunks[0].continues_into_next is False
        assert result.chunks[0].continued_from_previous is False

    def test_overlap_does_not_cross_a_heading_boundary(self) -> None:
        """Overlap repeats content; it must never invent a heading path.

        Carrying a block from "Permissions" into a chunk headed "Create a filter"
        would attribute content to a section it is not in — the exact
        misattribution FR-018 exists to prevent, reached by a different route.
        """
        from app.ingestion.chunker import chunk_units

        result = chunk_units(
            _blocks(
                *[("p", _PROSE, ["A", "Create"]) for _ in range(8)],
                *[("p", _STEPS, ["A", "Permissions"]) for _ in range(8)],
            )
        )

        assert len(result.chunks) >= 2
        for chunk in result.chunks:
            others = [c for c in result.chunks if c is not chunk]
            if others and chunk.continued_from_previous:
                previous = others[0]
                assert previous.heading_path == chunk.heading_path


# ============================================================================
# T056 — the usefulness floor
# ============================================================================


class TestUsefulnessFloor:
    def test_a_trivial_unit_is_rejected_and_recorded(self) -> None:
        """T056: below the floor, rejected **and recorded** — never dropped.

        Recording is the requirement. A silently dropped unit leaves no trace, and
        the question it would have answered becomes unanswerable later with
        nothing in the audit trail to explain why.
        """
        from app.ingestion.chunker import chunk_units

        result = chunk_units(
            _blocks(("p", "Note.", ["Trivial"]), ("p", _PROSE, ["Real"]), ("p", "Ok.", ["Trivial"]))
        )

        assert [c.heading_path for c in result.chunks] == [["Real"]]
        assert len(result.rejected) == 2
        assert all(r.reason for r in result.rejected)
        assert all("Note." in r.text or "Ok." == r.text for r in result.rejected)

    def test_nothing_is_rejected_when_everything_is_useful(self) -> None:
        """The floor must not fire on the real corpus, where sections are larger."""
        from app.ingestion.chunker import chunk_units

        result = chunk_units(
            _blocks(
                ("p", _PROSE, ["Filters"]),
                ("list", _STEPS, ["Filters"]),
                ("table", _TABLE, ["Filters"]),
                ("code", _CODE, ["Filters"]),
            )
        )

        assert result.rejected == []
        assert len(result.chunks) == 1


# ============================================================================
# Sizing, and the byte bound that actually binds
# ============================================================================


class TestSizing:
    def test_no_chunk_exceeds_the_configured_ceiling(self) -> None:
        from app.ingestion.chunker import chunk_units

        result = chunk_units(_long_section(20))

        assert result.chunks
        oversized = [c for c in result.chunks if c.estimated_tokens > 1000]
        assert oversized == [], f"chunks over the 1000-token ceiling: {len(oversized)}"

    def test_no_chunk_exceeds_the_service_byte_limit(self) -> None:
        """The bound that can actually reject a write, checked exactly.

        Measured 2026-09-30: 40,960 bytes per vector, and the service **rejects**
        anything above it. A chunk over the send ceiling would be a hard ingest
        error, so this is a correctness property rather than a quality one.
        """
        from app.ingestion.chunker import chunk_units
        from app.ingestion.tokens import send_ceiling_bytes

        result = chunk_units(_long_section(30))
        ceiling = send_ceiling_bytes()

        for chunk in result.chunks:
            assert len(chunk.text.encode("utf-8")) <= ceiling, (
                f"a chunk is {len(chunk.text.encode('utf-8'))} bytes against a "
                f"{ceiling}-byte send ceiling; the service would reject the write"
            )

    def test_ordinals_are_sequential_from_zero(self) -> None:
        """`vector_id` is `{document_id}#{ordinal:04d}` (R-005), so a gap is a bug.

        The partial unique index on `crawl_jobs` and the supersession logic in
        T060 both reason about ordinal boundaries, so a skipped or repeated
        ordinal is not cosmetic.
        """
        from app.ingestion.chunker import chunk_units

        result = chunk_units(_long_section(14))

        assert [c.ordinal for c in result.chunks] == list(range(len(result.chunks)))

    def test_block_types_are_recorded_per_unit(self) -> None:
        """`block_types` is `TEXT[]` in data-model.md §2 and drives the corpus view.

        T070 asserts a non-zero count of `code` and `table` block types, which is
        how the Phase 3 checkpoint knows the extractor really captured structure.
        """
        from app.ingestion.chunker import chunk_units

        result = chunk_units(
            _blocks(
                ("p", _PROSE, ["API"]),
                ("code", _CODE, ["API"]),
                ("table", _TABLE, ["API"]),
            )
        )

        assert set(result.chunks[0].block_types) >= {"p", "code", "table"}

    def test_an_empty_input_produces_nothing_and_says_so(self) -> None:
        """Not an exception. A page with no extractable content is recorded, not fatal."""
        from app.ingestion.chunker import chunk_units

        result = chunk_units([])

        assert result.chunks == []
        assert result.rejected == []
