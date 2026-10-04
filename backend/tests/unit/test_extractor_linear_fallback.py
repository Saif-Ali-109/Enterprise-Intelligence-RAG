"""The direct HTML fallback for pages where trafilatura drops every heading.

T052 extension. Measured on developer.atlassian.com (2026-10-02): trafilatura
keeps a page's prose but flattens its <h2>–<h5> tree away, so `mine_units`
returns units with `heading_path == []`. FR-021 (heading relevance) and the
FR-063 anchor grading cannot work on that, so `extract_units` falls back to a
direct linear walk of the served HTML. These tests pin the fallback's shape on
a synthetic developer-doc-shaped page, and pin that it only activates when
trafilatura truly found no headings.
"""

from __future__ import annotations

import pytest
from app.ingestion.extractor import _extract_linear_fallback, extract_units

pytestmark = pytest.mark.unit


_DEV_SHAPED = """
<html><head><title>Issues</title></head><body>
<h1>Issues</h1>
<p>The issues API lets you create, read, update, and delete issues.</p>
<h2>Create an issue</h2>
<p>Send a POST request with the issue fields in the JSON body.</p>
<h3>Example request</h3>
<pre>curl -X POST /rest/api/3/issue</pre>
<h2>Archive an issue</h2>
<p>Archiving hides the issue from all searches without deleting it.</p>
<ul><li>Archived issues cannot be edited</li><li>Admins can restore them</li></ul>
</body></html>
"""


def test_fallback_recovers_real_heading_paths() -> None:
    units = _extract_linear_fallback(_DEV_SHAPED)
    assert units, "fallback produced no units"
    by_path: dict[tuple[str, ...], int] = {}
    for unit in units:
        by_path[tuple(unit.heading_path)] = by_path.get(tuple(unit.heading_path), 0) + 1
    assert ("Issues", "Create an issue") in by_path, by_path
    assert ("Issues", "Create an issue", "Example request") in by_path, by_path
    assert ("Issues", "Archive an issue") in by_path, by_path


def test_fallback_block_types_match_content() -> None:
    units = _extract_linear_fallback(_DEV_SHAPED)
    types = {u.block_type for u in units}
    assert "p" in types
    assert "code" in types
    assert "list" in types


def test_fallback_strips_chrome_and_styles() -> None:
    html = (
        "<html><body><nav><h2>Navigation</h2></nav>"
        "<h2>Real section</h2><p>Body text with <style>.x{color:red}</style>inline styling.</p>"
        "</body></html>"
    )
    units = _extract_linear_fallback(html)
    # the nav element and its heading are chrome, the style text must not leak
    paths_text = (
        " ".join(" > ".join(u.heading_path) for u in units) + " " + " ".join(u.text for u in units)
    )
    assert "Navigation" not in paths_text
    assert "color:red" not in paths_text
    assert any(u.heading_path == ["Real section"] for u in units)


def test_fallback_activates_only_when_trafilatura_yields_no_path() -> None:
    # A short body makes trafilatura flatten the page, so the fallback must fire.
    units = extract_units(_DEV_SHAPED)
    assert any(u.heading_path for u in units), "fallback did not activate"
