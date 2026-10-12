"""Navigation is not content (measured 2026-10-09, FR-032).

One page of the developer documentation produced 449 extracted blocks, of which
309 carried no sentence-terminated line and the median block was six words: the
site header, the breadcrumb, the reference index, and three thousand words of
resource names. They were embedded, indexed, retrieved, and cited — a published
answer asserting "the latest released version of the Jira Cloud platform API is
version 2" was grounded in a breadcrumb containing the string `REST API v2`.

The interesting fixtures are the prose blocks that carry **no** terminator,
because the extractor cuts paragraphs at element boundaries and a paragraph can
end mid-sentence. A rule keyed on length alone would throw away real
documentation; `test_prose_cut_mid_sentence_survives` is the guard for exactly
that, and it is the test that would fail first if the rule were ever loosened.

Every fixture is text taken from a live page.
"""

from __future__ import annotations

import pytest
from app.core.config import Settings
from app.ingestion.chunker import _is_navigation_block, chunk_units
from app.ingestion.extractor import ExtractedBlock

pytestmark = pytest.mark.unit

# --- text taken from live pages ---------------------------------------------

#: `developer.atlassian.com/cloud/jira/platform/rest/v2/api-group-issues/`:
#: the site menu, which that page carries three times before any content.
SITE_MENU = "\n".join(
    [
        "Get support",
        "Sign in",
        "Developer",
        "Documentation",
        "Cloud",
        "Data Center",
        "Resources",
        "News and Updates",
        "REST API v2",
        "Reference",
        "Authentication",
        "Permissions",
        "Timestamps",
        "Status codes",
        "Errors",
        "Pagination",
        "Webhooks",
        "Changelog",
    ]
)

#: The same page: the reference index — 453 words of resource names, no
#: sentence anywhere in it.
RESOURCE_INDEX = "\n".join(
    [
        "Announcement banner",
        "App data policies",
        "App migration",
        "App properties",
        "Application roles",
        "Audit records",
        "Avatars",
        "Classification levels",
        "Dashboards",
        "Dynamic modules",
        "Field schemes",
        "Filter sharing",
        "Filters",
        "Group and user picker",
        "Groups",
        "Issue attachments",
        "Issue comment properties",
        "Issue comments",
        "Issue custom field associations",
    ]
    * 3
)

#: The same page: the breadcrumb the version claim was grounded in.
BREADCRUMB = "Cloud Jira Cloud platform / Reference / REST API v2"

#: `developer.atlassian.com/cloud/jira/platform/rate-limiting/`.
LAST_UPDATED = "Last updated Oct 9, 2026"

#: `support.atlassian.com/confluence-cloud/docs/create-a-space/`: real
#: documentation, cut mid-sentence, with no full stop at the end of a line.
CUT_MID_SENTENCE = (
    "Spaces in Confluence are designed to suit different purposes. When you create a "
    "new space, you choose the kind that matches how your team works, and you can "
    "change it later"
)

#: The same page, immediately afterwards.
CUT_MID_WORD = (
    "A space key is a short unique identifier that forms part of the URL for a space. "
    "By default the key is derived from the space name, and it cannot be changed"
)

#: The same page: a list of constraint labels. Content-adjacent, and dropped.
CONSTRAINT_LABELS = "Unique\nComposed of only alphanumeric characters\nNo more than 255 characters"

#: `support.atlassian.com/jira-cloud-administration/docs/manage-permissions-in-jira-cloud/`.
SKIP_LINK = "Skip to main content"

#: Ordinary prose, for the control.
PROSE = (
    "Open the filter manager and choose Save as filter. A saved filter can be run "
    "again by name from the same screen."
)


def block_of(text: str, block_type: str = "p", heading: list[str] | None = None) -> ExtractedBlock:
    return ExtractedBlock(
        block_type=block_type,
        text=text,
        heading_path=heading if heading is not None else ["Documentation"],
    )


@pytest.mark.parametrize(
    ("label", "text", "block_type"),
    [
        ("the site menu", SITE_MENU, "p"),
        ("the reference index", RESOURCE_INDEX, "list"),
        ("a constraint label list", CONSTRAINT_LABELS, "list"),
    ],
)
def test_page_furniture_is_not_indexed(label: str, text: str, block_type: str) -> None:
    """The blocks that made the corpus unusable, and the claim they supported."""
    assert _is_navigation_block(block_of(text, block_type)), f"{label} must not be indexed"


@pytest.mark.parametrize(
    ("label", "text"),
    [
        ("prose cut at a sentence boundary", CUT_MID_SENTENCE),
        ("prose cut mid-word", CUT_MID_WORD),
        ("ordinary prose", PROSE),
    ],
)
def test_prose_survives(label: str, text: str) -> None:
    """The regression this rule could plausibly have caused, and did not.

    Neither of the first two fixtures contains a full stop at the end of a line,
    which is most of what the rule has to go on. Both are documentation a reader
    would want cited.
    """
    assert not _is_navigation_block(block_of(text)), f"{label} must survive"


def test_a_crawl_timestamp_is_not_content() -> None:
    """A side effect worth stating: the corpus stops carrying crawl-time noise.

    Not why the rule exists, but "Last updated Oct 9, 2026" is a fact about the
    crawl, not about the documentation, and it was embedded and retrievable as
    though it were the latter.
    """
    assert _is_navigation_block(block_of(LAST_UPDATED))


def test_an_empty_block_is_rejected() -> None:
    """No text at all must not become an empty vector."""
    assert _is_navigation_block(block_of("   \n  "))


def test_the_rule_can_be_switched_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """Configuration, not a hard-coded judgement.

    An operator indexing a site whose navigation *is* the content — a glossary, a
    style guide, an API index read as a glossary — needs the blocks this rule
    removes. The setting exists for that and defaults on, because the measured
    default was wrong.
    """
    from app.core import config as config_module

    original = config_module.get_settings

    def _off() -> Settings:
        return original().model_copy(update={"chunk_drop_navigation": False})

    monkeypatch.setattr(config_module, "get_settings", _off)

    assert not _is_navigation_block(block_of(SITE_MENU))


def test_navigation_is_dropped_before_assembly_not_after() -> None:
    """Where the rule sits, and why the placement is the whole difference.

    Blocks are joined into 600-1000 token units, so a unit almost always contains
    at least one block that ends in a full stop. A unit-level test answered on
    `any()` therefore keeps the entire unit — navigation included — which is what
    the first version of this rule did, and why it removed almost nothing.

    The fixture is one real page's shape: menu, breadcrumb, a paragraph, another
    menu, and a second paragraph. Assembled, that is a single unit containing a
    sentence; filtered per unit it stays whole.
    """
    blocks = [
        block_of(SITE_MENU),
        block_of(BREADCRUMB),
        block_of(CUT_MID_SENTENCE, heading=["Issues"]),
        block_of(SITE_MENU),
        block_of(CUT_MID_WORD, heading=["Issues"]),
    ]
    result = chunk_units(blocks)

    navigation_rejections = [r for r in result.rejected if r.reason == "navigation_boilerplate"]
    assert len(navigation_rejections) == 3
    assert all(r.heading_path == ["Documentation"] for r in navigation_rejections)

    # And the content survived into a unit that carries neither menu nor crumb.
    assert result.chunks, "the page's prose must still produce a unit"
    for chunk in result.chunks:
        assert SITE_MENU not in chunk.text
        assert BREADCRUMB not in chunk.text


def test_reasons_stay_a_closed_countable_set() -> None:
    """`skipped_reasons` is a breakdown; an uncounted reason makes it a lie.

    Every rejection names an enumerated value, because the crawl sums them per
    source and an operator deciding whether the corpus looks right can only do
    that if each drop is attributable to a cause.
    """
    result = chunk_units(
        [
            block_of(SITE_MENU),
            block_of(BREADCRUMB),
            block_of("x " * 5),  # below the usefulness floor once assembled
            block_of(CUT_MID_SENTENCE, heading=["Issues"]),
            block_of(CUT_MID_WORD, heading=["Issues"]),
        ]
    )
    reasons = {r.reason for r in result.rejected}
    assert reasons <= {"navigation_boilerplate", "below_usefulness_floor"}
    assert "navigation_boilerplate" in reasons
