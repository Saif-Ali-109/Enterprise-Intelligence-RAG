"""Structure-aware chunking, and the usefulness floor.

T055, T056. FR-015, FR-016, FR-017, FR-018, R-003, R-013.

## The rule, and the arithmetic behind it

**Split on structure, size on estimate.** A new unit begins at a heading change,
or — when a single section is too large — at a block boundary *within* it. Never
at a character count, which FR-015 prohibits outright. The token estimate
decides only *whether* a boundary is needed, never *where* it goes.

Blocks arrive from the extractor already in document order and already carrying
the heading path they sit under, so this module does not re-derive structure. It
groups, measures, and splits.

## The decision that mattered: do not merge small sections

A natural-looking optimisation is to merge adjacent 200-token sections to reach
the 600-token target. It is wrong here, and the reason is specific rather than
aesthetic: the merged unit can carry only **one** heading path. FR-018 requires
each unit to retain its *full* heading path, and FR-021 scores heading relevance
against that path. A unit that cites "Create a filter" while containing the
contents of "Permissions" too is a citation pointing at the wrong place on a
real page — the specific failure this system exists to prevent.

So a section below the target stays its own unit. Measured over 60 blocks from 6
real Atlassian pages, 16 of 17 sections fall under the 600-token target, which
makes "do not merge" the *common* path rather than the exceptional one.

## What the floor is for

T056's usefulness floor rejects and **records** units too small to retrieve. The
recording is the requirement: a silently dropped unit leaves a question
unanswerable later with nothing in the audit trail to explain the gap.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.core.logging import get_logger
from app.ingestion.extractor import ExtractedBlock
from app.ingestion.tokens import (
    byte_size,
    chunk_token_bounds,
    estimate_tokens,
    send_ceiling_bytes,
)

_log = get_logger("ingestion.chunker")


@dataclass(frozen=True, slots=True)
class Chunk:
    """One retrievable unit."""

    ordinal: int
    text: str
    heading_path: list[str] = field(default_factory=list)
    block_types: list[str] = field(default_factory=list)
    #: The blocks this unit was built from. Kept so overlap can be verified
    #: against provenance rather than by searching for a repeated substring, and
    #: so T060's supersession can tell which blocks survived a re-crawl.
    blocks: list[ExtractedBlock] = field(default_factory=list)
    estimated_tokens: int = 0
    byte_size: int = 0
    continued_from_previous: bool = False
    continues_into_next: bool = False
    #: Set when the unit is over a size bound and was emitted anyway because no
    #: safe split existed. Never silent: the service will reject the write, and
    #: the record is what makes that explainable later.
    oversized: bool = False

    def display_heading(self) -> str:
        """The heading path as one string, for rendering.

        Joins only here, at the point a string is actually needed. Comparing one
        unit's path against the next to find a section boundary needs lists, and
        a joined string invites a comparison against a separator this function
        chose.
        """
        return " > ".join(self.heading_path)


@dataclass(frozen=True, slots=True)
class RejectedUnit:
    """A unit that was not indexed, and why.

    `reason` is a stable, machine-comparable value — not a sentence for a human.
    The reasons are enumerated so T061 can count them per crawl and report a
    total, which is the only way "some units were dropped" becomes visible.
    """

    text: str
    heading_path: list[str]
    reason: str
    estimated_tokens: int


@dataclass(frozen=True, slots=True)
class ChunkResult:
    """Everything one page produced: what was indexed, and what was not."""

    chunks: list[Chunk] = field(default_factory=list)
    rejected: list[RejectedUnit] = field(default_factory=list)
    oversized: list[Chunk] = field(default_factory=list)


def _section_key(block: ExtractedBlock) -> tuple[str, ...]:
    return tuple(block.heading_path)


def _assemble(ordinal: int, blocks: list[ExtractedBlock], *, oversized: bool) -> Chunk:
    """Build a `Chunk` from an ordered run of blocks.

    Blocks are joined with a blank line. That is a *separator*, not a split: it
    preserves the paragraph boundary the reader sees, and it is what makes
    adjacent prose blocks distinguishable inside one unit. Nothing decides a
    boundary here.
    """
    text = "\n\n".join(b.text.strip() for b in blocks if b.text.strip())
    # `p` is the dominant type for prose, so it is the sensible divisor when a
    # section is mixed. The estimate is a floor either way.
    return Chunk(
        ordinal=ordinal,
        text=text,
        heading_path=list(blocks[0].heading_path),
        block_types=sorted({b.block_type for b in blocks}),
        blocks=list(blocks),
        estimated_tokens=estimate_tokens(text, block_type=blocks[0].block_type),
        byte_size=byte_size(text),
        oversized=oversized,
    )


def _fits(blocks: list[ExtractedBlock]) -> bool:
    """Whether adding `blocks` would keep the unit inside both bounds.

    Both, and both matter. The token ceiling is a quality target. The **byte**
    ceiling is a hard one: 40,960 bytes per vector, above which the service
    rejects the write outright (measured, T027). Checking only tokens would let
    a table — measured at 1.38 chars/token, the densest real content — pass as
    "1,000 tokens" and arrive as 6,898 bytes of a 20,480-byte budget.
    """
    text = "\n\n".join(b.text.strip() for b in blocks if b.text.strip())
    if not text:
        return False
    if byte_size(text) > send_ceiling_bytes():
        return False
    _, max_tokens = chunk_token_bounds()
    return estimate_tokens(text, block_type=blocks[0].block_type) <= max_tokens


def _is_undersized(chunk: Chunk) -> bool:
    """Whether a unit is below the usefulness floor (T056)."""
    from app.core.config import get_settings

    return chunk.estimated_tokens < get_settings().chunk_min_useful_tokens


def _overlap_tail(
    blocks: list[ExtractedBlock], previous: list[ExtractedBlock]
) -> list[ExtractedBlock]:
    """The trailing blocks of `previous` worth repeating, without overshooting.

    FR-016 asks for deliberate overlap so a fact near a boundary is retrievable
    from either neighbour. What it must **not** do is cross a heading boundary:
    repeating a block from "Permissions" inside a unit headed "Create a filter"
    attributes content to a section it is not in, which is the misattribution
    FR-018 exists to prevent reached by a different route. The caller only ever
    offers `previous` blocks that share the unit's heading path, so that cannot
    happen here — the guarantee is by construction, not by check.
    """
    _, overlap_tokens = chunk_token_bounds()[0], get_overlap()
    if overlap_tokens <= 0:
        return []

    # Walk backwards from the end, accumulating until the budget is spent.
    tail: list[ExtractedBlock] = []
    running = 0
    for block in reversed(previous):
        cost = estimate_tokens(block.text, block_type=block.block_type)
        if tail and running + cost > overlap_tokens:
            break
        tail.insert(0, block)
        running += cost
    return tail


def get_overlap() -> int:
    from app.core.config import get_settings

    return get_settings().chunk_overlap_tokens


def _split_oversized(
    blocks: list[ExtractedBlock], path: tuple[str, ...]
) -> tuple[list[Chunk], list[Chunk]]:
    """Split one section that is too large, returning `(fits, oversized)`.

    Never enters a block. Every block is atomic — a paragraph, a list, a table, a
    code example — and FR-017 makes that a requirement for code specifically: a
    code example cut in half is a syntax error, and the half that got indexed
    would be cited for a claim it does not contain.

    A **single** block larger than the ceiling has no safe boundary, so it is
    emitted whole and flagged. FR-017 says "when a safe boundary is available";
    when none is, keeping the example intact and recording the overflow is the
    only option that does not destroy it, and the record is what makes the
    eventual service rejection explainable rather than mysterious.
    """
    fits: list[Chunk] = []
    oversized: list[Chunk] = []
    current: list[ExtractedBlock] = []

    for block in blocks:
        if not _fits([block]):
            # Atomic and over the bound on its own. Nothing to split it into.
            oversized.append(_assemble(0, [block], oversized=True))
            continue

        if current and not _fits(current + [block]):
            fits.append(_assemble(0, current, oversized=False))
            # Carry the tail of what was just emitted into the next unit.
            current = _overlap_tail(current, current) + [block]
        else:
            current.append(block)

    if current:
        fits.append(_assemble(0, current, oversized=False))

    return fits, oversized


def chunk_units(blocks: list[ExtractedBlock]) -> ChunkResult:
    """Split `blocks` into retrievable units along their real structure.

    Three rules, in order:

    1. **Group by heading path.** Consecutive blocks sharing a path form a
       section, and a section is never merged with a neighbour (see the module
       docstring — a merged unit could carry only one of the two paths).
    2. **Split a section only if it is too large**, at block boundaries, with
       deliberate overlap between the resulting neighbours.
    3. **Reject and record** anything below the usefulness floor.

    Ordinals are assigned here, sequentially from zero, because
    `vector_id = {document_id}#{ordinal:04d}` (R-005) and T060's supersession
    reconciles on ordinal boundaries.
    """
    if not blocks:
        return ChunkResult()

    min_tokens, _ = chunk_token_bounds()
    if min_tokens <= 0:  # pragma: no cover - guarded by settings validation
        _log.error("chunk_min_useful_tokens is not positive; no unit can be judged")
        return ChunkResult()

    sections: list[list[ExtractedBlock]] = []
    current: list[ExtractedBlock] = []
    current_key: tuple[str, ...] | None = None

    for block in blocks:
        if not block.text.strip():
            # An empty block is not a unit. Recording it would produce an entry
            # in the audit trail that says nothing, for content that was never
            # there.
            continue
        key = _section_key(block)
        if key != current_key:
            if current:
                sections.append(current)
            current = [block]
            current_key = key
        else:
            current.append(block)
    if current:
        sections.append(current)

    chunks: list[Chunk] = []
    rejected: list[RejectedUnit] = []
    oversized: list[Chunk] = []

    for section in sections:
        path = _section_key(section[0])
        if _fits(section):
            candidates = [_assemble(0, section, oversized=False)]
            section_oversized: list[Chunk] = []
        else:
            candidates, section_oversized = _split_oversized(section, path)

        # The oversized units are **indexed as well as recorded**. An earlier
        # version put them only in `result.oversized`, which made them
        # disappear from the corpus: recorded, warned about, and then never
        # emitted, so the content was silently gone while the log said it had
        # been handled.
        #
        # Emitting them is right even though the service will reject the write.
        # The write failing loudly at ingest is the outcome we want, and it is
        # the *only* stage that can see the record and act on it. Suppressing
        # the unit here instead would leave a hole in the corpus with no error
        # anywhere, which is the failure mode FR-031 and T060 exist to prevent.
        # The flag travels with the chunk so the indexing stage can decide what
        # to do rather than having the decision made for it and forgotten.
        for candidate in section_oversized:
            chunks.append(candidate)
            oversized.append(candidate)

        for candidate in candidates:
            if _is_undersized(candidate):
                rejected.append(
                    RejectedUnit(
                        text=candidate.text,
                        heading_path=list(candidate.heading_path),
                        reason="below_usefulness_floor",
                        estimated_tokens=candidate.estimated_tokens,
                    )
                )
                _log.info(
                    "unit rejected as below the usefulness floor",
                    extra={
                        "estimated_tokens": candidate.estimated_tokens,
                        "heading_path": candidate.heading_path,
                    },
                )
                continue
            chunks.append(candidate)

    # Ordinals last, over what actually survived, so they are dense and the
    # `vector_id` they form has no gaps. A rejected unit consumes no ordinal:
    # the vector that would have carried it was never written.
    numbered: list[Chunk] = []
    for position, chunk in enumerate(chunks):
        numbered.append(
            Chunk(
                ordinal=position,
                text=chunk.text,
                heading_path=chunk.heading_path,
                block_types=chunk.block_types,
                blocks=chunk.blocks,
                estimated_tokens=chunk.estimated_tokens,
                byte_size=chunk.byte_size,
                continued_from_previous=chunk.continued_from_previous,
                continues_into_next=chunk.continues_into_next,
                oversized=chunk.oversized,
            )
        )

    # Mark the overlap relationships now that the sequence is final, since they
    # are a property of a unit's neighbours rather than of the unit itself.
    #
    # Derived by intersecting *block identity* (by text) rather than by searching
    # for a repeated substring in the assembled text. Two blocks can share text
    # by accident — a one-word list item repeated on a page is common — and a
    # substring search would report overlap that was never created. Overlap can
    # only ever have been introduced within one heading path, because
    # `_overlap_tail` is only ever called with blocks from the section being
    # split.
    with_flags: list[Chunk] = []
    for position, chunk in enumerate(numbered):
        previous = numbered[position - 1] if position > 0 else None
        following = numbered[position + 1] if position < len(numbered) - 1 else None

        shares_with_previous = (
            previous is not None
            and previous.heading_path == chunk.heading_path
            and bool(_block_texts(previous) & _block_texts(chunk))
        )
        shares_with_following = (
            following is not None
            and following.heading_path == chunk.heading_path
            and bool(_block_texts(following) & _block_texts(chunk))
        )

        with_flags.append(
            Chunk(
                ordinal=chunk.ordinal,
                text=chunk.text,
                heading_path=chunk.heading_path,
                block_types=chunk.block_types,
                blocks=chunk.blocks,
                estimated_tokens=chunk.estimated_tokens,
                byte_size=chunk.byte_size,
                continued_from_previous=shares_with_previous,
                continues_into_next=shares_with_following,
                oversized=chunk.oversized,
            )
        )

    if rejected:
        _log.info(
            "units rejected at the usefulness floor",
            extra={"rejected": len(rejected), "indexed": len(with_flags)},
        )
    if oversized:
        _log.warning(
            "units over a size bound and emitted unsplit; the service will reject "
            "the write unless these are handled",
            extra={"oversized": len(oversized), "indexed": len(with_flags)},
        )

    return ChunkResult(chunks=with_flags, rejected=rejected, oversized=oversized)


def _block_texts(chunk: Chunk) -> set[str]:
    """The text of each block a chunk was built from, for overlap comparison."""
    return {b.text.strip() for b in chunk.blocks if b.text.strip()}


def overlap_tokens(chunk: Chunk, previous: Chunk | None) -> int:
    """Estimated tokens `chunk` repeats from `previous` (FR-016, auditable).

    Measured from provenance — the two units' block texts compared directly —
    rather than by searching for a repeated substring. The number the registry
    stores is therefore what actually repeats, and a unit whose overlap was
    dropped for crossing a section boundary reports zero instead of an
    optimistic estimate.
    """
    if previous is None:
        return 0
    shared = _block_texts(previous)
    total = 0
    for block in chunk.blocks:
        text = block.text.strip()
        if text and text in shared:
            total += estimate_tokens(block.text, block_type=block.block_type)
    return total


__all__ = ["Chunk", "ChunkResult", "RejectedUnit", "chunk_units", "overlap_tokens"]
