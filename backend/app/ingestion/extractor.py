"""Main-content extraction and heading-path reconstruction.

T052. FR-018, FR-015, FR-029, R-009, R-010.

Two stages, and the split is the design. trafilatura does what only
trafilatura can do — decide which part of a page is main content and throw the
rest away — and this module does what trafilatura cannot, because the library
destroys the information: rebuild the heading hierarchy it flattened.

## Why the walk exists at all

trafilatura's XML output emits headings as **flat siblings** of paragraphs,
tagged `<head rend="h1">`…`<head rend="h6">`. The level survives in `rend`; the
hierarchy does not exist in the output. R-009 records this, and it is
load-bearing for the whole project: FR-018 requires every chunk to carry its
`heading_path`, and FR-015 requires chunk boundaries derived from real document
structure. A flat heading list satisfies neither.

The walk is three lines, and the interesting part is which failure each one
prevents:

```python
del stack[level - 1:]   # close deeper levels before pushing
stack.append(text)
```

Without the `del`, two `h2` siblings under one `h1` produce
`["Configure a board", "Permissions"]` as a *path*, which reads as a section
nested inside another. It is not. Every citation built on that path would send a
reader to a heading that does not exist.

## What the walk deliberately does not do

**It does not invent structure.** A page opening with prose before its first
heading gets `heading_path == []`, not `["Introduction"]` and not the page title.
FR-018 asks for the path the unit actually sits under, and a fabricated path is
worse than an empty one: an empty path is visibly empty, a fabricated one looks
authoritative and points nowhere.

**It does not read prose as structure.** A skipped level (`h1` then `h3`) closes
the deeper levels and yields `["A", "C"]` — the two headings that exist. It does
not pad the gap with a placeholder, because a path containing `""` renders as a
dangling separator in a citation and groups as a different section.

**It does not trust `rend` to be well-formed.** `_level_of` clamps to 1–6 and
falls back to a documented default when the attribute is missing or malformed.
The stack is indexed by level, so an `h7` or a missing `rend` would either
overflow or silently shift every subsequent path. The measured range is `h1`–`h6`
and nothing else, but a parser that breaks on the sixth-and-a-half value is a
parser that breaks in production.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal

from app.core.errors import InternalError
from app.core.logging import get_logger

_log = get_logger("ingestion.extractor")

BlockType = Literal["p", "list", "table", "code", "quote"]

#: R-009: valid `output_format` values are `csv, html, json, markdown, python,
#: txt, xml, xmltei`. `"md"` is **not** one of them and raises. `xml` is chosen
#: over `markdown` because markdown output derives its `#` markers from `rend`
#: and defaults to level 2 when `rend` is absent, which would make the level —
#: the one thing this whole module depends on — unreliable.
_OUTPUT_FORMAT = "xml"

#: The measured heading-level range. Used to clamp, not to validate: a level
#: outside the range is a library change or a malformed document, and either way
#: the walk must keep producing correct paths for everything after it.
_MIN_LEVEL = 1
_MAX_LEVEL = 6

#: Applied when `rend` is missing or unparseable. The deepest level, because
#: guessing shallow would place a unit at the top of the document when it
#: actually sits under headings — a wrong citation, whereas the deep guess only
#: costs a path that is too specific. R-009 notes markdown output uses this same
#: fallback and that is precisely why markdown is not used here.
_DEFAULT_LEVEL = 2

_REND = re.compile(r"^h(\d+)$", re.IGNORECASE)

#: A block whose children are its own content. Emitting a `list` and then its
#: `item`s as separate blocks would inflate the unit count with fragments that
#: have no meaning alone; R-009 lists these as one-block constructs.
_CONTAINER_TAGS = frozenset({"list", "table", "code", "quote"})

#: Inline leaves that carry text. `lb` and `hi` are trafilatura's own marks for
#: line breaks and highlighted spans, and they matter for `code` blocks.
_TEXT_TAGS = frozenset({"p", "item", "cell", "lb", "hi", "ref", "head"})

#: heading tags the direct fallback walk recognises, in any of h1-h6.
_RAW_HEADINGS = re.compile(r"<h[1-6]\b", re.IGNORECASE)

#: Elements in the linear fallback that never produce content blocks. Without
#: dropping them, a page's navigation and chrome <h2>s would become headings and
#: the path they feed to every subsequent block would be invented structure.
_FALLBACK_SKIP_TAGS = frozenset({"nav", "header", "footer", "aside", "script", "style", "noscript"})

_FALLBACK_BLOCK_TAGS = frozenset({"p", "pre", "table", "blockquote"})
_FALLBACK_HEADING_TAGS = frozenset({f"h{n}" for n in range(1, 7)})


def _raw_html_has_headings(html: str) -> bool:
    return bool(_RAW_HEADINGS.search(html))


@dataclass(frozen=True, slots=True)
class ExtractedBlock:
    """One content block with the heading path it sits under.

    `heading_path` is a list rather than a joined string because the chunker
    groups consecutive units into one chunk by comparing paths, and a joined
    string invites a comparison against a separator that the joiner chose.
    Joining is the chunker's decision, at the point where the string is about to
    be displayed.
    """

    block_type: BlockType
    text: str
    heading_path: list[str] = field(default_factory=list)
    #: The level of the heading that most recently opened this block's path.
    #: Recorded so section detection (T057) can tell "still under `h2`" from
    #: "moved to a sibling `h2`" without re-deriving it from the path's length,
    #: which is not the same thing when a level was skipped.
    heading_level: int = 0


def _level_of(attributes: dict[str, Any]) -> int:
    """The heading level from a `rend` attribute, clamped and defaulted.

    Every branch here is defensive against a value the walk would otherwise
    index with, and each costs one comparison against a bug where a single
    malformed attribute shifts the heading path of every unit after it — a
    failure that produces no error, only confidently wrong citations.
    """
    rend = attributes.get("rend")
    if not isinstance(rend, str):
        return _DEFAULT_LEVEL

    match = _REND.match(rend.strip())
    if match is None:
        return _DEFAULT_LEVEL

    return max(_MIN_LEVEL, min(int(match.group(1)), _MAX_LEVEL))


def _normalise_heading(text: str) -> str:
    """Collapse the source page's own whitespace inside a heading.

    Not cosmetic. A heading written across two lines in the HTML arrives with a
    newline, and the same section would then produce two different
    `heading_path` values — so grouping units by path, which is how the chunker
    decides a section ended, would split that section into two chunks and cite
    it twice.
    """
    return " ".join(text.split())


def _push_heading(stack: list[str], stack_levels: list[int], *, level: int, heading: str) -> None:
    """Close deeper levels, then push — and drop a level that repeats its parent.

    The first two lines are R-009's walk. The third is measured from real pages,
    not anticipated.

    `support.atlassian.com` frequently repeats the `h1` text as the first `h2`:
    `<h1>Access a space</h1>` then `<h2>Access a space</h2>`. Carried through
    literally, that produces `["Access a space", "Access a space"]` — a path
    that reads as a section nested inside itself and that names no real
    location in the document. Every unit beneath it would carry it, and a reader
    following the citation would be sent to a heading pair that does not exist.

    Skipped when the levels differ, so this is not a de-duplication of the
    heading *list*: `# A` / `## A` is one section named twice, and collapsing
    it keeps the level structure that the stack depends on. Two genuinely
    distinct `h2` sections that share a name are unaffected, because they are at
    the same level and only the deepest level is ever compared.
    """
    while stack_levels and stack_levels[-1] >= level:
        stack_levels.pop()
        stack.pop()

    if not (stack and stack[-1] == heading):
        stack.append(heading)
        stack_levels.append(level)


def _text_of(element: Any) -> str:
    """All visible text under `element`, with block boundaries respected.

    R-009: `<code>` can nest `<code>`, so a naive recursive `itertext()` walk
    double-counts a code block's own text. The recursion is also skipped for
    container tags the caller handles as a unit, and separators are inserted
    between block-level leaves so a two-line code sample does not come back as
    one run-on line.
    """
    parts: list[str] = []

    def walk(node: Any) -> None:
        tag = node.tag if isinstance(node.tag, str) else ""
        if node.text:
            parts.append(node.text)

        for child in node:
            child_tag = child.tag if isinstance(child.tag, str) else ""
            # A nested container inside a container is its parent's content, and
            # emitting it as a unit would duplicate the text.
            if child_tag in _CONTAINER_TAGS and tag in _CONTAINER_TAGS:
                walk(child)
                continue
            walk(child)

        if node.tag in _TEXT_TAGS and child_last_was_block(parts):
            parts.append("\n")
        if node.tail:
            parts.append(node.tail)

    def child_last_was_block(buffer: list[str]) -> bool:
        # Only separate when something was actually appended, so an empty
        # element does not contribute a line of its own.
        return bool(buffer) and bool(buffer[-1].strip())

    walk(element)
    text = "".join(parts)
    # Normalise the inter-block newlines, but never the newlines inside a code
    # block: a sample that reads `def f():` on the same line as its body is
    # broken code, and this walk must not be what breaks it.
    return re.sub(r"[ \t]+\n", "\n", text).strip()


def extract_main_content(html: str, *, base_url: str | None = None) -> str:
    """Run trafilatura and return its normalized XML body.

    Raises `InternalError` when the library returns nothing, rather than
    returning an empty string. The distinction matters downstream: an empty
    string is a page whose content was successfully determined to be empty, and
    T051 needs to *record* that as a rejection with a reason (FR-029). Conflating
    the two would make a parsing failure look like a legitimate exclusion, and
    the difference between "this page is not worth indexing" and "we could not
    read this page" is worth an operator's attention.

    `InternalError` and not a new code: the 14-code vocabulary in
    `contracts/README.md` is closed by design, and a parse failure is a fault in
    this service handling a request it accepted — which is what that code means.
    Inventing a fifteenth code for a distinction no client can act on would widen
    a contract to describe a log line.
    """
    import trafilatura

    kwargs: dict[str, Any] = {
        "output_format": _OUTPUT_FORMAT,
        "include_links": False,
        "include_tables": True,
        "include_comments": False,
        "favor_recall": True,
    }
    if base_url is not None:
        kwargs["url"] = base_url

    try:
        extracted = trafilatura.extract(html, **kwargs)
    except Exception as exc:  # noqa: BLE001
        # trafilatura raises a wide and undocumented set of exceptions on
        # malformed input. Catching broadly is right here — the caller's next
        # move is a recorded rejection either way — and the specific type goes
        # in the log, because "it failed" and "it failed on a byte-order mark"
        # call for different attention.
        _log.error(
            "main content extraction failed",
            extra={"error_type": type(exc).__name__},
        )
        raise InternalError("The page could not be parsed for main content.") from exc

    if not extracted or not extracted.strip():
        raise InternalError("The page yielded no main content.")

    return extracted


def _iter_body_blocks(xml: str) -> list[tuple[str, dict[str, str], Any]]:
    """Every block in the `<main>` subtree, in document order, flattened one level.

    lxml only (R-010): trafilatura hands back lxml elements, so there is no
    conversion step and no third parser. The single level of flattening is
    deliberate — `list`/`table`/`quote`/`code` are one block whose children are
    its content, per R-009.
    """
    from lxml import etree

    try:
        root = etree.fromstring(xml.encode("utf-8"), parser=etree.XMLParser(recover=True))
    except etree.XMLSyntaxError as exc:
        raise InternalError("The extracted main content is not parseable XML.") from exc

    main = root.find(".//main")
    container = main if main is not None else root

    blocks: list[tuple[str, dict[str, str], Any]] = []
    for child in container:
        tag = child.tag if isinstance(child.tag, str) else ""
        if tag == "head":
            blocks.append(("head", dict(child.attrib), child))
        elif tag in _CONTAINER_TAGS:
            blocks.append((tag, dict(child.attrib), child))
        elif tag == "p":
            blocks.append(("p", dict(child.attrib), child))
    return blocks


def reconstruct_heading_paths(
    blocks: list[tuple[str, dict[str, str], Any]] | list[tuple[str, dict[str, Any], str]],
) -> list[ExtractedBlock]:
    """Walk flat blocks and rebuild each one's heading path (R-009).

    Accepts either lxml elements or plain strings as the third tuple member, so
    the walk can be tested against hand-built input without standing up a
    parser. The production path passes elements; the tests pass strings, and a
    signature that only accepted one of them would make the walk untestable
    without a fixture that reproduces trafilatura's exact output shape.
    """
    stack: list[str] = []
    stack_levels: list[int] = []
    units: list[ExtractedBlock] = []

    for tag, attributes, payload in blocks:
        if tag == "head":
            level = _level_of(attributes)
            heading = _normalise_heading(
                _text_of(payload) if not isinstance(payload, str) else payload
            )
            if not heading:
                # An empty heading carries no path component. Pushing it would
                # put a blank level into every path beneath it.
                continue
            # Close every level at or deeper than this one, then push. The
            # `del stack[level-1:]` is the line R-009 is about: without it,
            # sibling headings nest.
            _push_heading(stack, stack_levels, level=level, heading=heading)
            continue

        text = _text_of(payload) if not isinstance(payload, str) else payload
        text = text.strip()
        if not text:
            # A container that rendered to nothing is not a unit. Recording it
            # would produce an empty chunk, which fails the usefulness floor
            # later and costs a record in the audit trail for no information.
            continue

        units.append(
            ExtractedBlock(
                block_type=tag,  # type: ignore[arg-type]
                text=text,
                heading_path=list(stack),
                heading_level=stack_levels[-1] if stack_levels else 0,
            )
        )

    return units


def _extract_linear_fallback(html: str) -> list[ExtractedBlock]:
    """Walk the raw HTML when trafilatura flattened away every heading.

    Measured on developer.atlassian.com (2026-10-02 probe): the served HTML
    carries rich, server-rendered `<h2>`–`<h5>` trees, yet trafilatura's XML
    drops every one of them (`extract_units` then returns units with no
    `heading_path`, which FR-018 accepts but leaves every citation unanchored).
    When that combination is detected, this fallback rebuilds the heading stack
    and content blocks directly from the served HTML, using the same push
    semantics as `reconstruct_heading_paths`. The trade is cleaner text for
    real structure: trafilatura's boilerplate filtering is bypassed, so page
    chrome (site title as `<h1>`, login panels, footer links) may ride along.
    It is a fallback, not a replacement, and it never runs for pages where
    trafilatura already recovered headings.
    """

    from lxml import etree

    try:
        root = etree.fromstring(html.encode("utf-8"), parser=etree.HTMLParser(recover=True))
    except Exception as exc:  # noqa: BLE001
        raise InternalError("The page could not be parsed for main content.") from exc
    if root is None:
        raise InternalError("The page could not be parsed for main content.")

    stack: list[str] = []
    stack_levels: list[int] = []
    units: list[ExtractedBlock] = []

    def _text_of_el(element: Any) -> str:
        # inline <style>/<script> elements ride along inside page fragments and
        # their CSS/JS must never become heading text or block text.
        parts: list[str] = []

        def collect(el: Any) -> None:
            tag = el.tag if isinstance(el.tag, str) else ""
            if tag in ("style", "script", "noscript"):
                if el.tail:
                    parts.append(el.tail)
                return
            if el.text:
                parts.append(el.text)
            for child in el:
                collect(child)
            if el.tail:
                parts.append(el.tail)

        collect(element)
        return " ".join(" ".join(parts).split())

    def _emit(tag: str, element: Any) -> None:
        text = _text_of_el(element)
        if not text:
            return
        block_type: BlockType
        if tag == "pre":
            block_type = "code"
        elif tag == "blockquote":
            block_type = "quote"
        elif tag == "table":
            block_type = "table"
        elif tag in ("ul", "ol"):
            block_type = "list"
        else:
            block_type = "p"
        units.append(
            ExtractedBlock(
                block_type=block_type,
                text=text,
                heading_path=list(stack),
                heading_level=stack_levels[-1] if stack_levels else 0,
            )
        )

    def _has_block_descendant(element: Any) -> bool:
        for descendant in element.iter():
            tag = descendant.tag if isinstance(descendant.tag, str) else ""
            if tag in _FALLBACK_BLOCK_TAGS or tag in _FALLBACK_HEADING_TAGS or tag in ("ul", "ol"):
                return True
        return False

    def _walk(element: Any) -> None:
        tag = element.tag if isinstance(element.tag, str) else ""
        if tag in _FALLBACK_SKIP_TAGS:
            return
        if tag in _FALLBACK_HEADING_TAGS:
            level = int(tag[1])
            heading = _normalise_heading(_text_of_el(element))
            if heading:
                _push_heading(stack, stack_levels, level=level, heading=heading)
            return
        if tag in ("ul", "ol"):
            items = [_normalise_heading(_text_of_el(li)) for li in element.iter("li")]
            text = "\n".join(item for item in items if item)
            if text:
                units.append(
                    ExtractedBlock(
                        block_type="list",
                        text=text,
                        heading_path=list(stack),
                        heading_level=stack_levels[-1] if stack_levels else 0,
                    )
                )
            return
        if tag in _FALLBACK_BLOCK_TAGS:
            _emit(tag, element)
            return
        if tag in ("div", "section", "article", "main", "body") and not _has_block_descendant(
            element
        ):
            if _text_of_el(element):
                _emit("p", element)
            return
        for child in element:
            _walk(child)

    body = root.find(".//body")
    start = body if body is not None else root
    for child in start:
        _walk(child)

    return units


def extract_units(html: str, *, base_url: str | None = None) -> list[ExtractedBlock]:
    """The whole of T052: trafilatura, then the walk.

    Convenience rather than necessity — `extract_main_content` and
    `reconstruct_heading_paths` are both used separately (the first by the
    boilerplate check in T051, the second by tests) — but the pipeline always
    wants the two in sequence, and a caller assembling the order by hand is a
    caller who can get it backwards and index a body with no heading paths.

    `developer.atlassian.com` is the measured exception: trafilatura keeps the
    text but flattens every heading, so the usual path returns units with no
    heading path. The service still needs per-unit paths for FR-018/FR-021 and
    one source must never silently degrade the shared evaluation (FR-063), so
    when that combination occurs — trafilatura found no headings, yet the
    served HTML has some — fall back to a direct linear walk of the raw HTML
    (Text of the page keeps whatever it could; headings come back).
    """
    xml = extract_main_content(html, base_url=base_url)
    units = reconstruct_heading_paths(_iter_body_blocks(xml))
    if not any(b.heading_path for b in units) and _raw_html_has_headings(html):
        return _extract_linear_fallback(html)
    return units


__all__ = [
    "BlockType",
    "ExtractedBlock",
    "extract_main_content",
    "extract_units",
    "reconstruct_heading_paths",
]
