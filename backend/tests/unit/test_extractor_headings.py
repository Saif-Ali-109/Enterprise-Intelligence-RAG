"""Heading-path reconstruction from trafilatura's flattened output.

T041. FR-018, FR-015, R-009. **The highest-risk test in the ingestion stage**,
because trafilatura destroys the heading hierarchy and FR-018 requires every
chunk to carry the path it sat under. A wrong walk does not fail loudly: it
produces plausible-looking, flat, or wrong-nested paths, and every citation in
the finished system points a reader at the wrong place in a real document.

The walk itself is three lines. The value of this file is that the walk is
tested against **trafilatura's real output**, produced here by calling the
library — not against a hand-written XML fixture shaped like what the library
usually emits. That distinction was not academic: the first version of these
tests used a short fixture, and trafilatura discarded every heading in it,
silently returning one concatenated `<p>`. A hand-written fixture would have
passed while the real pipeline produced no heading paths at all.

## Two measured properties that shape every test here

**1. Headings are flat siblings, and `rend` is the only hierarchy that survives.**
Confirmed: `<head rend="h1">Config</h1><p>…</p><head rend="h2">Syntax</head>`. No
nesting, and `rend` is always `h1`…`h6` — never absent, never another shape. The
tests assert the walk on that real output, and one test pins the `rend` range
because a level-7 heading would index past the stack.

**2. Below roughly three sentences, trafilatura returns no headings at all.**
It falls back to concatenating the whole page into one `<p>`, which is a
different behaviour from "a page with no headings" and must not be read as one.
`test_a_page_too_short_for_extraction_yields_no_heading_path` pins this, and
T051's boilerplate rejection is what keeps such pages out of the corpus.
"""

from __future__ import annotations

import re

import pytest

pytestmark = pytest.mark.unit

#: Realistic Atlassian-shaped prose. Degenerate fixtures like "body body body"
#: produce artefacts — repeated words make trafilatura emit a repeated heading
#: block — so the padding is varied to keep the measurements about the library's
#: behaviour rather than the fixture's.
_PROSE = [
    "A board filter is a saved query that defines a subset of issues on a board.",
    "Open the board and choose Filters from the view options in the sidebar.",
    "Filter syntax follows the JQL grammar, which is documented as its own topic.",
    "Only board administrators may edit a filter that is shared with the board.",
]

_NAV = '<nav><a href="/x">Home</a><a href="/y">Documentation</a></nav>'
_FOOTER = "<footer><p>Copyright 2026 Atlassian</p></footer>"


def _page(article: str) -> str:
    """A full page wrapping `article` in the boilerplate real pages carry.

    The nav and footer are not decoration. trafilatura decides which part of a
    document is main content, and a fragment without them takes a different code
    path — one where headings are dropped. A fixture that omits the noise
    measures the wrong thing.
    """
    return (
        "<html><head><title>Filter configuration</title></head><body>"
        f"{_NAV}<main><article>{article}</article></main>{_FOOTER}</body></html>"
    )


def _para(text: str, repeats: int = 6) -> str:
    """A paragraph long enough to survive extraction."""
    return f"<p>{(text + ' ') * repeats}</p>"


def _heads(xml: str) -> list[tuple[str, str]]:
    return re.findall(r'<head rend="([^"]+)">([^<]*)', xml)


# ============================================================================
# The measurement itself — asserted before the walk is trusted
# ============================================================================


class TestTrafilaturaOutputShape:
    def test_headings_are_flat_siblings_and_rend_carries_the_level(self) -> None:
        """The premise of R-009, asserted rather than assumed.

        If trafilatura ever nested `<head>` elements, the stack walk would be
        reconstructing something that was already there — and, worse, would keep
        producing the same answers for the wrong reason.
        """
        from app.ingestion.extractor import extract_main_content

        xml = extract_main_content(
            _page(
                "<h1>Configure board filters</h1>"
                + _para(_PROSE[0])
                + "<h2>Create a filter</h2>"
                + _para(_PROSE[1])
                + "<h3>Filter syntax</h3>"
                + _para(_PROSE[2])
            )
        )

        assert _heads(xml) == [
            ("h1", "Configure board filters"),
            ("h2", "Create a filter"),
            ("h3", "Filter syntax"),
        ]
        # Flat: no `<head>` element contains another.
        assert "<head" in xml and "<head" not in re.sub(r"<head[^>]*>[^<]*</head>", "", xml)

    def test_rend_is_always_h1_to_h6(self) -> None:
        """The stack is indexed by level, so the level range is a real bound.

        `h1`…`h6` confirmed by generating a page with all six levels. An `h7`
        would make `del stack[level - 1:]` index past the end, which is why
        `_level_of` clamps rather than trusting the attribute.
        """
        from app.ingestion.extractor import extract_main_content

        article = "".join(f"<h{i}>Level {i}</h{i}>{_para(_PROSE[i % 4])}" for i in range(1, 7))
        xml = extract_main_content(_page(article))

        assert [rend for rend, _ in _heads(xml)] == ["h1", "h2", "h3", "h4", "h5", "h6"]

    def test_boilerplate_is_removed(self) -> None:
        """R-009's other half, and the reason trafilatura is used at all.

        A "See also" list at the end of a documentation page is exactly the kind
        of block that would otherwise become a chunk of links. Asserted because
        a regression here is invisible in output: the unit would simply acquire
        an unhelpful heading path.
        """
        from app.ingestion.extractor import extract_main_content

        xml = extract_main_content(
            _page(
                "<h1>Configure board filters</h1>"
                + _para(_PROSE[0])
                + "<h2>See also</h2>"
                + "<ul><li><a href='/a'>Related article one</a></li>"
                + "<li><a href='/b'>Related article two</a></li></ul>"
            )
        )

        assert "See also" not in xml
        assert "Related article" not in xml


# ============================================================================
# The walk
# ============================================================================


class TestHeadingPathWalk:
    async def test_a_nested_path_is_reconstructed(self) -> None:
        """The ordinary case: h1, h2, h3 in order."""
        from app.ingestion.extractor import reconstruct_heading_paths

        units = reconstruct_heading_paths(
            [
                ("head", {"rend": "h1"}, "Jira Cloud"),
                ("p", {}, _PROSE[0]),
                ("head", {"rend": "h2"}, "Configure a board"),
                ("p", {}, _PROSE[1]),
                ("head", {"rend": "h3"}, "Add a filter"),
                ("p", {}, _PROSE[2]),
            ]
        )

        assert [u.heading_path for u in units] == [
            ["Jira Cloud"],
            ["Jira Cloud", "Configure a board"],
            ["Jira Cloud", "Configure a board", "Add a filter"],
        ]

    async def test_returning_to_a_shallower_level_truncates_the_path(self) -> None:
        """`del stack[level - 1:]` — the line the whole note is about.

        Two h2 siblings under one h1: the second must not inherit "Create a
        filter" from the first. This is the test that fails first if the walk is
        a plain append.
        """
        from app.ingestion.extractor import reconstruct_heading_paths

        units = reconstruct_heading_paths(
            [
                ("head", {"rend": "h1"}, "Jira Cloud"),
                ("head", {"rend": "h2"}, "Configure a board"),
                ("p", {}, _PROSE[0]),
                ("head", {"rend": "h2"}, "Permissions"),
                ("p", {}, _PROSE[1]),
            ]
        )

        assert units[0].heading_path == ["Jira Cloud", "Configure a board"]
        assert units[1].heading_path == ["Jira Cloud", "Permissions"]

    async def test_a_skipped_level_does_not_invent_an_empty_slot(self) -> None:
        """h1 then h3, with no h2. The measured live-service case.

        A document that jumps levels must produce `("A", "C")`, not a path with a
        blank where B would have been. An implementation that pads the stack to
        the current level passes this by looking right and produces a path
        containing an empty string, which then renders as a dangling separator
        in the citation.
        """
        from app.ingestion.extractor import reconstruct_heading_paths

        units = reconstruct_heading_paths(
            [
                ("head", {"rend": "h1"}, "Advanced"),
                ("p", {}, _PROSE[0]),
                ("head", {"rend": "h3"}, "Orphaned subheading"),
                ("p", {}, _PROSE[1]),
            ]
        )

        assert units[0].heading_path == ["Advanced"]
        assert units[1].heading_path == ["Advanced", "Orphaned subheading"]
        # No empty slot anywhere in the path.
        assert all(part.strip() for part in units[1].heading_path)

    async def test_content_before_the_first_heading_has_an_empty_path(self) -> None:
        """Real pages open with an introductory paragraph.

        Inventing a path for it — "Introduction", or the page title — would be
        fabrication, and FR-018 asks for the path the unit actually sits under.
        Empty is the honest value, and it is distinguishable from a path that
        was never reconstructed.
        """
        from app.ingestion.extractor import reconstruct_heading_paths

        units = reconstruct_heading_paths(
            [
                ("p", {}, _PROSE[0]),
                ("head", {"rend": "h1"}, "Configure a board"),
                ("p", {}, _PROSE[1]),
            ]
        )

        assert units[0].heading_path == []
        assert units[1].heading_path == ["Configure a board"]

    async def test_a_heading_does_not_become_its_own_unit(self) -> None:
        """A heading is a path component, not content.

        Emitting headings as units would roughly double the unit count and fill
        the corpus with chunks that are a title and nothing else — retrievable,
        citable, and useless.
        """
        from app.ingestion.extractor import reconstruct_heading_paths

        units = reconstruct_heading_paths(
            [
                ("head", {"rend": "h1"}, "Configure a board"),
                ("p", {}, _PROSE[0]),
                ("head", {"rend": "h2"}, "Permissions"),
            ]
        )

        assert len(units) == 1
        assert units[0].block_type == "p"

    async def test_repeated_sibling_headings_at_the_same_level(self) -> None:
        """The measured duplicate-heading case, and it is not hypothetical.

        A page can legitimately repeat a heading (a "Example" per feature). The
        path must reset rather than nest: `["A", "Example", "Example"]` would be
        a path that does not exist in the document.
        """
        from app.ingestion.extractor import reconstruct_heading_paths

        units = reconstruct_heading_paths(
            [
                ("head", {"rend": "h1"}, "Filters"),
                ("head", {"rend": "h2"}, "Example"),
                ("p", {}, _PROSE[0]),
                ("head", {"rend": "h2"}, "Example"),
                ("p", {}, _PROSE[1]),
            ]
        )

        assert units[0].heading_path == ["Filters", "Example"]
        assert units[1].heading_path == ["Filters", "Example"]

    async def test_a_heading_repeating_its_parent_does_not_nest(self) -> None:
        """Measured on `access-a-project`, a real page: h1 text repeated as h2.

        `<h1>Access a space</h1>` then `<h2>Access a space</h2>`. Carried
        literally, every unit beneath gets a path of
        `["Access a space", "Access a space"]` — a section that reads as nested
        inside itself and names no real location on the page. The citation would
        look entirely plausible and point a reader nowhere useful.
        """
        from app.ingestion.extractor import reconstruct_heading_paths

        units = reconstruct_heading_paths(
            [
                ("head", {"rend": "h1"}, "Access a space"),
                ("p", {}, _PROSE[0]),
                ("head", {"rend": "h2"}, "Access a space"),
                ("p", {}, _PROSE[1]),
                ("head", {"rend": "h2"}, "View a space's work items"),
                ("p", {}, _PROSE[2]),
            ]
        )

        assert units[0].heading_path == ["Access a space"]
        assert units[1].heading_path == ["Access a space"]
        assert units[2].heading_path == ["Access a space", "View a space's work items"]

    async def test_two_distinct_same_level_headings_sharing_a_name_survive(self) -> None:
        """The counterpart, so the fix above cannot be a blanket de-duplication.

        Repeating a name is common; two genuinely different sections may still
        carry it, and collapsing those would merge sections that are separately
        citable. Only a *parent* repeating its *child's* level is collapsed.
        """
        from app.ingestion.extractor import reconstruct_heading_paths

        units = reconstruct_heading_paths(
            [
                ("head", {"rend": "h1"}, "Filters"),
                ("head", {"rend": "h2"}, "Example"),
                ("p", {}, _PROSE[0]),
                ("head", {"rend": "h2"}, "Example"),
                ("p", {}, _PROSE[1]),
            ]
        )

        assert units[0].heading_path == ["Filters", "Example"]
        assert units[1].heading_path == ["Filters", "Example"]

    async def test_headings_are_normalised_for_whitespace_only(self) -> None:
        """Heading text arrives with the source page's own spacing.

        Indentation and newlines inside a `<head>` element are formatting, not
        content. Left in, two units under the same heading get two different
        `heading_path` values, and grouping by path — which is how a chunker
        finds where one section ends — silently splits the section in two.
        """
        from app.ingestion.extractor import reconstruct_heading_paths

        units = reconstruct_heading_paths(
            [
                ("head", {"rend": "h1"}, "  Configure \n a board  "),
                ("p", {}, _PROSE[0]),
                ("head", {"rend": "h1"}, "Configure\ta\nboard"),
                ("p", {}, _PROSE[1]),
            ]
        )

        assert units[0].heading_path == units[1].heading_path
        assert units[0].heading_path == ["Configure a board"]


# ============================================================================
# The measured short-page behaviour — a different thing from "no headings"
# ============================================================================


class TestMeasuredPageDepth:
    """What the walk must cope with, taken from real pages rather than fixtures.

    Measured 2026-09-30 across 14 real Jira documentation pages (R-009
    amendment). These tests do not assert that the corpus is deep — it is not.
    They assert that depth 1 and depth 4 go through the same walk, so a
    pipeline cannot quietly assume the shallow pages are the exception.
    """

    async def test_a_flat_h1_then_h2_page_needs_no_nesting(self) -> None:
        """9 of 14 real pages are exactly this: one `h1`, a flat run of `h2`.

        A walk that only ever tested three-deep nesting would pass on the
        fixtures and leave these pages' paths unverified.
        """
        from app.ingestion.extractor import reconstruct_heading_paths

        units = reconstruct_heading_paths(
            [
                ("head", {"rend": "h1"}, "Reopen a sprint"),
                ("p", {}, _PROSE[0]),
                ("head", {"rend": "h2"}, "To reopen a sprint"),
                ("p", {}, _PROSE[1]),
                ("head", {"rend": "h2"}, "Permissions"),
                ("p", {}, _PROSE[2]),
            ]
        )

        assert [u.heading_path for u in units] == [
            ["Reopen a sprint"],
            ["Reopen a sprint", "To reopen a sprint"],
            ["Reopen a sprint", "Permissions"],
        ]
        # No path ever exceeds depth 2 on such a page, and none implies nesting.
        assert max(len(u.heading_path) for u in units) == 2

    async def test_a_four_level_page_reconstructs_every_level(self) -> None:
        """The deepest real case measured: `reopen-a-sprint` reaches level 4.

        The full path and the sibling truncation at the deepest level, which is
        the case a stack walk gets wrong last and most visibly — a citation into
        the wrong sub-subsection of a troubleshooting page.
        """
        from app.ingestion.extractor import reconstruct_heading_paths

        units = reconstruct_heading_paths(
            [
                ("head", {"rend": "h1"}, "Reopen a sprint"),
                ("head", {"rend": "h2"}, "Scenarios for reopening sprints"),
                ("head", {"rend": "h3"}, "Simple scenarios"),
                ("p", {}, _PROSE[0]),
                ("head", {"rend": "h3"}, "Complex scenarios"),
                ("p", {}, _PROSE[1]),
            ]
        )

        assert units[0].heading_path == [
            "Reopen a sprint",
            "Scenarios for reopening sprints",
            "Simple scenarios",
        ]
        assert units[1].heading_path == [
            "Reopen a sprint",
            "Scenarios for reopening sprints",
            "Complex scenarios",
        ]
        # The shared parent is not duplicated by the truncation.
        assert units[1].heading_path[:2] == units[0].heading_path[:2]


class TestShortPageFallback:
    def test_a_page_too_short_for_extraction_yields_no_heading_path(self) -> None:
        """Measured: below ~3 sentences, trafilatura drops headings entirely.

        It returns one `<p>` containing the concatenated page — headings
        *flattened into prose*, not omitted. Reading that as "this page has no
        headings" would be a category error, and a pipeline that trusts it would
        index boilerplate under an empty path.

        This is the detection test for V2's risk, and it is why T051 rejects
        pages whose main content is too small (FR-029) rather than indexing them.
        """
        from app.ingestion.extractor import extract_main_content

        xml = extract_main_content(_page("<h1>Short page</h1><p>Too short.</p><p>Still short.</p>"))

        assert _heads(xml) == []
        # And it is not an empty result either — content came back, just flattened.
        assert xml.strip()
        assert "Short page" in xml

    def test_sufficient_length_keeps_its_structure(self) -> None:
        """The counterpart, so the test above cannot pass for a trivial reason.

        Without this, "no headings" would be satisfied by an extractor that
        never returns any, and the two tests would together prove nothing.
        """
        from app.ingestion.extractor import extract_main_content

        xml = extract_main_content(
            _page(
                "<h1>Configure a board</h1>"
                + "".join(_para(text) for text in _PROSE)
                + "<h2>Permissions</h2>"
                + _para(_PROSE[3])
            )
        )

        assert _heads(xml) == [("h1", "Configure a board"), ("h2", "Permissions")]
