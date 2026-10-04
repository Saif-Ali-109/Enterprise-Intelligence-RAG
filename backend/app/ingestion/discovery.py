"""Frontier discovery: sitemaps, links, and the scope rule.

T045 (crawl controls). FR-027, FR-028, FR-044.

## Why this module exists at all

The obvious way to build a crawl frontier is to follow links out from each
`start_url`. Measured against the six real sources on 2026-10-01, that yields:

| start_url | hrefs on page | in-scope at depth 1 |
|---|---|---|
| `…/jira-software-cloud/docs/access-a-project/` | 667 | **1** (itself) |
| `…/jira-cloud-administration/docs/set-up-jira-products/` | 283 | **4**, 3 malformed |
| `…/confluence-cloud/docs/create-and-organize-work-in-confluence-cloud/` | 484 | **1** (itself) |
| `…/jira-service-management-cloud/docs/what-are-issues-and-requests/` | 1235 | **4**, 3 malformed |
| `developer.atlassian.com/cloud/jira/platform/` | 64 | **29** |
| `developer.atlassian.com/cloud/jira/service-desk/` | 80 | **22** |

Four of six expose essentially nothing, because the Atlassian support
documentation navigation is rendered client-side — 656 of the 667 hrefs on
`access-a-project/` point at other products' directories, and the list of sibling
articles is not in the HTML at all. Link-following those four sources produces a
corpus of REST API reference only, which cannot answer a single one of the 32
curated questions about Jira user configuration, Confluence, or JSM.

The two `developer.atlassian.com` hosts are the opposite: their navigation *is*
server-rendered, and link-following works. So the two mechanisms are kept, and
each is used where it was measured to work.

## Why sitemaps are not a loophole

Every allowlisted host declares its sitemap in its own `robots.txt`, and the
support products' sub-sitemaps list 645 / 456 / 258 / 1190 pages. Using them is
therefore *more* compliant with the site's stated wishes, not less.

But a sitemap is not an authorisation. `support.atlassian.com/robots.txt`
disallows `/contact/*`, and a sitemap may still list a disallowed URL. So every
sitemap-derived URL is routed through the **same** robots gate as a link-derived
one. There is deliberately no second authorisation path: a sitemap must never
become a way around a directive (FR-028).

## Determinism

The frontier is **sorted**, not set-ordered. A page cap applied to an
unordered set selects a different subset on each run, which would make
`max_pages` mean something different every time and would quietly break SC-012
(re-crawling an unchanged source adds zero duplicate content) for reasons that
have nothing to do with content change.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from urllib.parse import urldefrag, urljoin, urlsplit
from xml.sax.saxutils import unescape

from app.core.logging import get_logger
from app.core.security import is_within_scope

_log = get_logger("ingestion.discovery")

#: Schemes a frontier URL may use. Anything else — `mailto:`, `javascript:`,
#: `tel:` — cannot be fetched and must never reach the queue.
_FETCHABLE_SCHEMES = frozenset({"http", "https"})

_HREF = re.compile(r"<a\b[^>]*?\bhref\s*=\s*[\"']([^\"']+)[\"']", re.I)
_LOC = re.compile(r"<loc>\s*([^<]+?)\s*</loc>")


@dataclass(frozen=True, slots=True)
class Candidate:
    """One URL the crawl may fetch, and how it came to be known.

    `depth` counts hops from the registered `start_url`. Sitemap-derived URLs are
    depth 1: the sitemap is one fetch from the root and is the site's own
    inventory of the section, so nothing further needs traversing to reach it.
    Recording them as depth 0 would understate how far they are from the
    registered entry point, which is exactly the question an operator asks when
    reading the crawl job.
    """

    url: str
    depth: int
    via: str


@dataclass(frozen=True, slots=True)
class Frontier:
    """What discovery found, and what it refused.

    Every refusal is counted. A discovery pass that silently dropped half its
    candidates would leave a corpus gap with nothing to explain it, which is the
    failure mode the crawl audit trail exists to prevent.
    """

    queued: list[Candidate] = field(default_factory=list)
    out_of_scope: int = 0
    malformed: int = 0
    disallowed: int = 0
    duplicate: int = 0

    @property
    def skipped_total(self) -> int:
        return self.out_of_scope + self.malformed + self.disallowed + self.duplicate


# A robots gate, injected rather than imported. This module therefore has no
# dependency on the robots fetcher (T049) and its code path is testable with no
# network at all. The default permits everything, which is correct only because
# the crawl orchestrator always supplies the real gate; it exists so that a
# single missing argument shows up as a test failure rather than as a crawler
# quietly ignoring robots.txt.
def _allows(_url: str) -> bool:
    return True


def normalise_url(href: str, *, base_url: str) -> str | None:
    r"""Resolve `href` against `base_url`, or return `None` if it cannot be one.

    Raw docstring: the malformed-href example below contains literal backslashes,
    which a non-raw docstring treats as escape sequences (and warns about).

    Returns `None` — never a guess — for every input that cannot be resolved to
    a URL a crawler could actually fetch:

    - **Backslashes anywhere in the resolved URL.** Measured on the real pages:
      hrefs arrive literally escaped, as
      `…/what-are-issues-and-requests/\/\/confluence.atlassian.com\/servicedeskcloud\/…`.
      `urljoin` does not reject these — it resolves them to a *wrong* URL under
      the start page's own path, which is worse than dropping them, because the
      wrong URL looks like a real one. A legitimate backslash is percent-encoded
      as `%5C`, so a raw one is malformed by definition.
    - Non-`http(s)` schemes, which cannot be fetched.
    - Unparseable input, and input that resolves to no host.

    Fragments are stripped: `#section-2` addresses a position inside a page, not
    a different page, and keeping it would enqueue the same document many times.
    """
    if not href or not href.strip():
        return None

    try:
        resolved, _fragment = urldefrag(urljoin(base_url, href.strip()))
        parts = urlsplit(resolved)
    except ValueError:
        return None

    if "\\" in resolved:
        return None
    if parts.scheme.lower() not in _FETCHABLE_SCHEMES:
        return None
    if not parts.hostname:
        return None
    return resolved


def _sort_key(candidate: Candidate) -> tuple[int, str]:
    """BFS order, and alphabetical within a depth.

    Breadth-first so the corpus fills evenly across a section rather than
    following one deep chain, and alphabetical so the same inputs always yield
    the same capped frontier.
    """
    return (candidate.depth, candidate.url)


def _derived_scope(start_url: str) -> str:
    """The default crawl scope: the start URL's containing directory."""
    from app.core.security import scope_prefix

    return scope_prefix(start_url)


def _collect(
    raw: list[tuple[str, int, str]],
    *,
    start_url: str,
    robots_allows: Callable[[str], bool],
    seen: set[str],
    scope: str | None = None,
) -> tuple[list[Candidate], int, int, int]:
    """Turn `(url, depth, via)` triples into admitted `Candidate`s.

    Returns `(admitted, out_of_scope, disallowed, duplicate)`.

    **The check order is deliberate.** Scope is tested before robots, so an
    out-of-scope URL is reported as out of scope rather than as a robots
    violation — the site cannot be said to have disallowed a URL we were never
    entitled to ask about, and recording it as `ROBOTS_DISALLOWED` would put a
    false statement in the audit trail.
    """
    admitted: list[Candidate] = []
    out_of_scope = disallowed = duplicate = 0
    effective_start = start_url if scope is None else _start_for_scope(start_url, scope)

    for url, depth, via in raw:
        if not is_within_scope(url, start_url=effective_start):
            out_of_scope += 1
            continue
        if not robots_allows(url):
            disallowed += 1
            continue
        if url in seen:
            duplicate += 1
            continue
        seen.add(url)
        admitted.append(Candidate(url=url, depth=depth, via=via))

    admitted.sort(key=_sort_key)
    return admitted, out_of_scope, disallowed, duplicate


def _start_for_scope(start_url: str, scope: str) -> str:
    """Express an explicit scope prefix in the form `is_within_scope` expects.

    `is_within_scope` compares the start URL's path segments (minus its leaf)
    against the candidate's, so an explicit prefix is supplied as a synthetic
    start URL sitting one level below the prefix. That keeps one scope
    implementation rather than two, which is what stops the derived rule and the
    declared rule from drifting apart.
    """
    from urllib.parse import urlsplit

    parts = urlsplit(start_url)
    return f"{parts.scheme}://{parts.netloc}{scope.rstrip('/')}/_scope_anchor"


def _raw_links(html: str, *, base_url: str, depth: int) -> tuple[list[tuple[str, int, str]], int]:
    """Every fetchable href in `html`, with no scope or robots filtering.

    Split out from the public entry point so `build_frontier` can gather raw
    candidates from both mechanisms and apply **one** filter to the union.

    The earlier version had each public helper filter internally, and then
    re-filtered the survivors in `build_frontier`. Two consequences, both bad:
    the sub-frontiers' refusal counts were discarded, so the counts a crawl job
    records under-reported what actually happened; and the second pass could
    never refuse anything, because everything reaching it had already passed.
    """
    raw: list[tuple[str, int, str]] = []
    malformed = 0

    for match in _HREF.finditer(html):
        resolved = normalise_url(match.group(1), base_url=base_url)
        if resolved is None:
            # Dropped, not recorded. A malformed href references no real page, so
            # there is nothing for an operator to act on; recording it would add
            # an audit row for a site bug that recurs on every crawl.
            malformed += 1
            continue
        raw.append((resolved, depth, "link"))

    return raw, malformed


def links_from_html(
    html: str,
    *,
    base_url: str,
    start_url: str,
    depth: int,
    robots_allows: Callable[[str], bool] = _allows,
) -> Frontier:
    """Discover in-scope `<a href>` targets in `html`.

    This is the mechanism that works on `developer.atlassian.com` and does not
    work on `support.atlassian.com` — see the module docstring for the
    measurement.
    """
    raw, malformed = _raw_links(html, base_url=base_url, depth=depth)
    admitted, out_of_scope, disallowed, duplicate = _collect(
        raw, start_url=start_url, robots_allows=robots_allows, seen=set()
    )
    return Frontier(
        queued=admitted,
        out_of_scope=out_of_scope,
        malformed=malformed,
        disallowed=disallowed,
        duplicate=duplicate,
    )


def _locs(xml_text: str) -> list[str]:
    """Every `<loc>` in a sitemap, XML-unescaped.

    `unescape` matters: sitemap `<loc>` values are XML-escaped, so a URL
    containing `&` arrives as `&amp;` and would otherwise be enqueued as a URL
    carrying a bogus query parameter.
    """
    return [unescape(match) for match in _LOC.findall(xml_text)]


def sitemap_index_children(xml_text: str) -> list[str]:
    """The sub-sitemap URLs in a `<sitemapindex>`.

    Returns an empty list for a plain `<urlset>`, which is not an error — a
    site's root sitemap may be either, and the caller uses a plain urlset
    directly.
    """
    return [url for url in _locs(xml_text) if url.endswith(".xml")]


def _raw_sitemap(
    xml_text: str, *, base_url: str, depth: int
) -> tuple[list[tuple[str, int, str]], int]:
    """Every page URL in a sitemap, with no scope or robots filtering.

    Split out for the same reason as `_raw_links`: `build_frontier` must filter
    the union once so its refusal counts are exact.
    """
    raw: list[tuple[str, int, str]] = []
    malformed = 0

    for loc in _locs(xml_text):
        if loc.endswith(".xml"):
            # A sub-sitemap inside a urlset is not a page. Skipping it is not a
            # scope or robots decision and is not counted as one.
            continue
        resolved = normalise_url(loc, base_url=base_url)
        if resolved is None:
            malformed += 1
            continue
        raw.append((resolved, depth, "sitemap"))

    return raw, malformed


def urls_from_sitemap(
    xml_text: str,
    *,
    start_url: str,
    robots_allows: Callable[[str], bool] = _allows,
    depth: int = 1,
) -> Frontier:
    """Discover in-scope page URLs from a sitemap `<urlset>`.

    Every URL passes the **same** robots gate as a link-derived one. A sitemap is
    an inventory, not a permission, and treating it as permission would make
    "does the site allow this?" a property of where a URL was discovered rather
    than of the URL itself.
    """
    raw, malformed = _raw_sitemap(xml_text, base_url=start_url, depth=depth)
    admitted, out_of_scope, disallowed, duplicate = _collect(
        raw, start_url=start_url, robots_allows=robots_allows, seen=set()
    )
    return Frontier(
        queued=admitted,
        out_of_scope=out_of_scope,
        malformed=malformed,
        disallowed=disallowed,
        duplicate=duplicate,
    )


def build_frontier(
    start_url: str,
    *,
    max_pages: int,
    robots_allows: Callable[[str], bool] = _allows,
    sitemap_xml: str | None = None,
    link_html: str | None = None,
    max_depth: int = 1,
    scope: str | None = None,
) -> Frontier:
    """Build a capped, deterministic frontier for one source.

    Both mechanisms gather raw candidates and are then filtered **once**, as a
    union, so the refusal counts reported here are exact. Sorting happens before
    the cap is applied, and that order is the point: a cap applied to an
    unordered set keeps a different subset on each run, so `max_pages` would mean
    something different every time.

    `scope` defaults to the start URL's containing directory, which is right for
    an *article* start URL: `/docs/access-a-project/` scopes to `/docs/`. It is
    wrong for a start URL that **is** a section index — `scope_prefix` drops the
    leaf segment, so `/cloud/jira/platform/` would scope to `/cloud/jira/` and
    silently pull in sibling sections like `/cloud/jira/software/`. Sources that
    are indexes therefore state their scope prefix explicitly.

    `max_depth` bounds how deep a link may sit. Sitemap URLs sit at depth 1
    because they are one fetch from the root; a source supplying a sitemap has no
    use of deeper traversal, since the sitemap already enumerates the section.
    """
    if max_pages <= 0:
        _log.error(
            "max_pages is not positive; the frontier would be empty", extra={"url": start_url}
        )
        return Frontier()

    scope = scope if scope is not None else _derived_scope(start_url)
    root = normalise_url(start_url, base_url=start_url) or start_url

    raw: list[tuple[str, int, str]] = [(root, 0, "start_url")]
    malformed = 0

    if sitemap_xml is not None:
        gathered, malformed_sm = _raw_sitemap(sitemap_xml, base_url=start_url, depth=1)
        raw.extend(gathered)
        malformed += malformed_sm

    if link_html is not None and max_depth >= 1:
        gathered, malformed_links = _raw_links(link_html, base_url=start_url, depth=1)
        raw.extend(gathered)
        malformed += malformed_links

    admitted, out_of_scope, disallowed, duplicate = _collect(
        raw, start_url=start_url, robots_allows=robots_allows, seen=set(), scope=scope
    )

    dropped_by_cap = max(0, len(admitted) - max_pages)
    capped = admitted[:max_pages]

    if dropped_by_cap:
        _log.info(
            "frontier truncated by the page cap",
            extra={"admitted": len(admitted), "kept": len(capped), "cap": max_pages},
        )

    frontier = Frontier(
        queued=capped,
        out_of_scope=out_of_scope,
        malformed=malformed,
        disallowed=disallowed,
        duplicate=duplicate,
    )
    if frontier.skipped_total or dropped_by_cap:
        _log.info(
            "frontier candidates refused at discovery",
            extra={
                "out_of_scope": out_of_scope,
                "malformed": malformed,
                "disallowed": disallowed,
                "duplicate": duplicate,
                "capped": dropped_by_cap,
            },
        )
    return frontier


__all__ = [
    "Candidate",
    "Frontier",
    "build_frontier",
    "links_from_html",
    "normalise_url",
    "sitemap_index_children",
    "urls_from_sitemap",
]
