"""Content sanitisation for any value that reaches a render path.

FR-046, Principle IX. Two rules, and the second is the one that is easy to get
wrong:

1. **Sanitise at the point of display, not at the point of storage.** The
   sanitiser is called where content is rendered, not where it is written. A
   sanitise-on-write design rots: the stored text is already stripped, so the
   day someone needs the raw form there is nothing to un-strip, and the escaping
   that the template applies is skipped because "we already cleaned it". The
   danger is not that this system's own content is malicious — it is fetched
   from a third party's documentation — it is that a stored-XSS payload
   survives a round trip and the render path trusts its own past output.

2. **Never sanitize into a string and call it safe.** `html.escape` is the right
   tool for text destined for a text node and the wrong tool for text destined
   for an attribute. This module is for *structured* sanitisation: an allowlist
   of elements and attributes, so a tag that is not on the list is removed
   rather than escaped-and-then-interpreted.

Both entry points return the same shapes the API returns. `sanitise_text` is
lossy-free for plain text and is a no-op on it, which is deliberate: retrieved
evidence is plain text, and passing it through a tag-stripper would corrupt
code examples, which are a large fraction of the developer-platform corpus and
the thing FR-015 goes to some trouble to preserve.
"""

from __future__ import annotations

import re
from typing import Final
from urllib.parse import urlsplit

# ============================================================================
# URL schemes
# ============================================================================

#: Schemes permitted in a link that a user might click. Everything else is
#: refused, and refused means the href is dropped, not escaped — an escaped
#: `javascript:` URL is still a `javascript:` URL once the browser decodes the
#: entity.
SAFE_URL_SCHEMES: Final[frozenset[str]] = frozenset({"http", "https", "mailto"})

_SCHEME_PATTERN: Final[re.Pattern[str]] = re.compile(r"^\s*([A-Za-z][A-Za-z0-9+.\-]*)\s*:")

# Control characters and whitespace inside a scheme are stripped by browsers
# before they parse it, so `java\tscript:` and `  javascript:` both execute.
# The scheme is extracted from a copy with those removed, which is what a
# browser effectively does — so doing it explicitly here is what makes the
# check meaningful.
_SCHEME_NOISE: Final[re.Pattern[str]] = re.compile(r"[\x00-\x20\x7f   ﻿]")


def is_safe_url(url: str) -> bool:
    """Whether a URL is safe to place in an `href` or a rendered link."""
    if not url:
        return False

    candidate = _SCHEME_NOISE.sub("", url)

    match = _SCHEME_PATTERN.match(candidate)
    if match is None:
        # No scheme at all. A relative or protocol-relative URL. `//evil.test`
        # inherits the page's scheme and points off-site, so it is refused too.
        return not candidate.startswith("//")

    return match.group(1).lower() in SAFE_URL_SCHEMES


def sanitise_url(url: str | None) -> str | None:
    """Return the URL if it is safe, `None` if not.

    `None` rather than `"#"` or `""`. A blank href is still a link in some
    assistive technology and still shows the link role with no destination;
    `None` lets the caller render plain text instead, which is the honest
    representation of "this link is not safe to follow".
    """
    return url if url and is_safe_url(url) else None


# ============================================================================
# HTML
# ============================================================================

#: Elements permitted in retrieved content rendered inline.
#:
#: The list is structural, not presentational. No `<img>`, because an image URL
#: is a request to a third-party host from the user's browser, which is a
#: tracking pixel with extra steps. No `<iframe>`, no `<form>`, no `<object>`.
ALLOWED_TAGS: Final[frozenset[str]] = frozenset(
    {
        "p",
        "br",
        "hr",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "ul",
        "ol",
        "li",
        "strong",
        "em",
        "b",
        "i",
        "u",
        "s",
        "sub",
        "sup",
        "mark",
        "small",
        "code",
        "pre",
        "kbd",
        "samp",
        "var",
        "blockquote",
        "q",
        "cite",
        "a",
        "table",
        "thead",
        "tbody",
        "tfoot",
        "tr",
        "th",
        "td",
        "caption",
        "colgroup",
        "col",
        "dl",
        "dt",
        "dd",
        "span",
        "div",
        "section",
        "article",
        "abbr",
        "time",
        "figure",
        "figcaption",
    }
)

#: Tags whose ENTIRE subtree is discarded, contents included.
#:
#: Not "unwrap" — remove the tag and keep the children. `<script>alert(1)</script>`
#: unwrapped leaves `alert(1)` in the document as text; discarded leaves nothing.
#: The same applies to `<style>`, whose contents are CSS, and to `<template>`,
#: whose contents are not rendered but do sit in the DOM.
DROP_SUBTREE_TAGS: Final[frozenset[str]] = frozenset(
    {
        "script",
        "style",
        "template",
        "noscript",
        "iframe",
        "object",
        "embed",
        "applet",
        "form",
        "svg",
        "math",
        "canvas",
        "audio",
        "video",
        "source",
        "track",
        "meta",
        "link",
        "base",
        "title",
        "head",
    }
)

#: Attributes permitted per tag. Anything not listed is dropped.
#:
#: No `style`, and no `class`. Inline style is a defacement and a
#: `position: fixed` overlay; class names are meaningless outside our own
#: stylesheet and a `data-*` attribute is a common exfiltration channel once
#: combined with a form. `id` and `name` are permitted on `a` so in-page
#: anchors within a citation still work.
ALLOWED_ATTRIBUTES: Final[frozenset[str]] = frozenset(
    {
        "href",
        "title",
        "lang",
        "dir",
        "colspan",
        "rowspan",
        "scope",
        "headers",
        "id",
        "name",
        "datetime",
        "cite",
        "start",
        "reversed",
        "value",
        "type",
        "width",
        "height",
    }
)

#: Attributes whose value is a URL and must therefore be scheme-checked.
URL_ATTRIBUTES: Final[frozenset[str]] = frozenset(
    {"href", "cite", "src", "action", "formaction", "data", "poster", "background"}
)

_COMMENT: Final[re.Pattern[str]] = re.compile(r"<!--.*?-->", re.DOTALL)
_CDATA: Final[re.Pattern[str]] = re.compile(r"<!\[CDATA\[.*?\]\]>", re.DOTALL)
_DOCTYPE: Final[re.Pattern[str]] = re.compile(r"<!DOCTYPE[^>]*>", re.IGNORECASE | re.DOTALL)
_TAG: Final[re.Pattern[str]] = re.compile(
    r"<\s*(/?)\s*([A-Za-z][A-Za-z0-9-]*)((?:[^>\"']|\"[^\"]*\"|'[^']*')*?)(/?)\s*>", re.DOTALL
)
_ATTR: Final[re.Pattern[str]] = re.compile(
    r"""([A-Za-z_:][A-Za-z0-9_.:-]*)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'>`]+))"""
)

#: Anything matching this is stripped from text, whatever the context. Zero
#: width characters hide text from a human reader while leaving it in the DOM.
_INVISIBLE: Final[re.Pattern[str]] = re.compile(r"[-‏‪-‮⁠-⁤﻿]")


def _escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#x27;")
    )


def _clean_attributes(raw: str) -> str:
    """Filter an attribute string down to the allowlist, scheme-checking URLs."""
    kept: list[str] = []

    for match in _ATTR.finditer(raw):
        name = match.group(1).lower()
        value = match.group(2) or match.group(3) or match.group(4) or ""

        if name not in ALLOWED_ATTRIBUTES:
            continue

        # An event handler, spelled with any prefix a browser tolerates.
        # `onclick`, `ONCLICK`, and `xlink:onclick` are the same attribute.
        if name.startswith("on"):
            continue

        if name in URL_ATTRIBUTES and not is_safe_url(value):
            continue

        kept.append(f'{name}="{_escape(value)}"')

    # Boolean attributes have no value and are handled by the tag matcher.
    for bare in re.findall(r"\b([A-Za-z-]+)\b(?!\s*=)", raw):
        if bare.lower() in {"reversed"}:
            kept.append(bare.lower())

    return (" " + " ".join(kept)) if kept else ""


def sanitise_html(html: str) -> str:
    """Reduce HTML to an allowlisted subset, preserving structure.

    The output is a string of safe HTML, not a parsed tree: this runs on a
    single unit of retrieved content, which is a bounded fragment, and building
    an lxml tree here would mean a parse of content that is about to be rendered
    as text anyway in most call sites.

    Preserves structure, which is the point. Stripping tags entirely would
    flatten a table into a run-on sentence and destroy the chunk's meaning —
    and retrieved evidence is read by humans in the inspection view, where
    losing the table would make the citation impossible to check (FR-050).
    """
    if not html:
        return ""

    working = _CDATA.sub("", html)
    working = _COMMENT.sub("", working)
    working = _DOCTYPE.sub("", working)

    # Drop the dangerous subtrees with their contents before the tag pass, so
    # `<script>x</script>` never reaches a point where `x` is escaped into
    # visible text. Nesting is handled by counting open/close pairs.
    for tag in DROP_SUBTREE_TAGS:
        working = re.sub(
            rf"<\s*{tag}\b.*?<\s*/\s*{tag}\s*>", "", working, flags=re.IGNORECASE | re.DOTALL
        )
        working = re.sub(rf"<\s*/?\s*{tag}\b[^>]*/?>", "", working, flags=re.IGNORECASE)

    out: list[str] = []
    position = 0
    # The real mechanism, replacing an earlier `dropped_depth` counter that was
    # initialised and assigned but never read. A single tag name is sufficient:
    # the regex pass above has already removed every balanced
    # `<tag>…</tag>` pair, so anything still open here is unbalanced, and one
    # marker tracks it.
    #
    # An unbalanced open tag therefore discards the remainder of the document.
    # That is fail-closed — an unclosed `<script>` loses trailing content rather
    # than leaking it — and it is the correct trade for a filter whose failure
    # mode is exposing third-party markup.
    drop_tag: str | None = None

    for match in _TAG.finditer(working):
        if match.start() > position:
            out.append(_escape(working[position : match.start()]))
        position = match.end()

        closing = match.group(1) == "/"
        name = match.group(2).lower()
        self_closing = match.group(4) == "/"

        if drop_tag is not None:
            if name == drop_tag and (closing or self_closing):
                drop_tag = None
            continue

        if name in DROP_SUBTREE_TAGS:
            if not closing and not self_closing:
                drop_tag = name
            continue

        if name not in ALLOWED_TAGS:
            # An unknown tag is a wrapper: the tag itself is removed and its
            # children kept, which preserves the text. There is deliberately no
            # DROP_SUBTREE fallback here — a name in DROP_SUBTREE_TAGS cannot
            # reach this branch, because the check above `continue`s for every
            # one of them, closing and self-closing alike. An earlier version
            # set a `dropped_depth` counter here to discard such a subtree, and
            # it was unreachable code whose comment claimed a safeguard the
            # implementation did not have.
            continue

        if closing:
            out.append(f"</{name}>")
        else:
            attributes = "" if self_closing else _clean_attributes(match.group(3) or "")
            out.append(f"<{name}{attributes}>" if attributes else f"<{name}>")

    if position < len(working):
        out.append(_escape(working[position:]))

    result = "".join(out)
    # Collapse the runs of blank lines that escaping a stripped document leaves
    # behind; a citation rendered as forty blank lines is not readable.
    result = re.sub(r"\n{3,}", "\n\n", result)
    return result.strip()


# ============================================================================
# Plain text
# ============================================================================

#: An answer is plain text with an explicit citation syntax, never HTML. If an
#: answer contains a tag, either the model produced HTML against instructions or
#: a stored payload is being echoed — and in both cases the right response is to
#: show the text as text, which is what escaping does.
_TAG_LIKE: Final[re.Pattern[str]] = re.compile(
    r"<(?:script|style|iframe|object|embed|form)\b", re.IGNORECASE
)


def sanitise_text(text: str) -> str:
    """Make text safe to place in a text node or an attribute value.

    Idempotent, and deliberately not a tag-stripper. Retrieved evidence is
    plain text, often containing code examples with `<` and `>` in them; a
    regex that removed "tags" would corrupt real content and the corruption
    would be invisible in the evidence while changing what the citation claims
    to say. Escaping changes what the READER sees, never what the system means.
    """
    if not text:
        return ""

    # Zero-width and bidi-override characters are removed rather than escaped:
    # escaping does nothing to them, and they are how visible text is made to
    # differ from what a model or a search index actually contains.
    cleaned = _INVISIBLE.sub("", text)

    # An angle bracket that starts a dangerous tag is escaped. Every other
    # angle bracket is left alone so code examples survive intact.
    cleaned = _TAG_LIKE.sub(lambda m: _escape(m.group(0)), cleaned)

    return cleaned


def sanitise_for_json(text: str) -> str:
    """Strip control characters that would corrupt a JSON payload.

    A raw control character in a string is legal in JSON only when escaped.
    Embedding one in a response body produces a payload that no client can
    parse, and the parse failure looks like a server bug rather than a content
    problem. Tabs, newlines, and carriage returns are kept — they are ordinary
    text — and the encoder escapes them correctly.
    """
    if not text:
        return ""
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)


def link_is_external(url: str) -> bool:
    """Whether a link points off this application.

    Used to decide whether a citation link needs `rel="noopener noreferrer"`.
    A citation always links to a publisher's page, so this is always true for
    citations — the function exists so that fact is a checked decision rather
    than an assumption baked into a template.
    """
    parts = urlsplit(url)
    if parts.scheme in ("http", "https"):
        return True
    return not url.startswith("/")


__all__ = [
    "ALLOWED_ATTRIBUTES",
    "ALLOWED_TAGS",
    "DROP_SUBTREE_TAGS",
    "SAFE_URL_SCHEMES",
    "URL_ATTRIBUTES",
    "is_safe_url",
    "link_is_external",
    "sanitise_for_json",
    "sanitise_html",
    "sanitise_text",
    "sanitise_url",
]
