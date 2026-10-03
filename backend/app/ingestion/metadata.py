"""Stage 5 metadata extraction.

T053. FR-024.

`title`, `product`, `category`, `page_type`, `language`, `canonical_link`
with a confidence value each — because each of those is a derived guess and
a retrieval or ranking decision that silently trusts it is a decision made
without knowing which derivations are honest. Confidence is asserted, not
imputed: if the title was read out of the page, it carries high confidence;
if it was inferred from the URL path because no <title> existed, it carries
low confidence and the caller (filter construction, evidence selection) is
expected to treat low-confidence metadata as a weak signal rather than a
fact (FR-021's metadata match prefers upheld values).

Product detection is deliberately prefix-based on the *source*, not the
host: the manifest declares which source owns a section, and every URL the
crawler grazes is under one of those sections (enforced upstream by
`assert_fetchable`/scope). Deriving product from the manifest's sections is
therefore deterministic. The URL path is used only when it disambiguates
*which* developer product a page belongs to.

Language is a small, honest heuristic, not a model: an English page is an
ASCII-dominated document with a stable English-stopword uplift. Anything
neither is returned as "unknown" with low confidence rather than a guess —
"never force classification" holds here too.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

# ---------------------------------------------------------------------------
# Confidence levels: recorded verbatim in metadata so downstream grading can
# separate "the page says it" from "we inferred it".
# ---------------------------------------------------------------------------
HIGH = 0.95
MEDIUM = 0.6
LOW = 0.3

_DOC_PATH_MARKERS = ("/docs/", "/guide/", "/documentation/")

_PRODUCT_BY_PATH: tuple[tuple[str, str], ...] = (
    ("/confluence-cloud/", "confluence"),
    ("/jira-service-management-cloud/", "jsm"),
    ("/jira-software-cloud/", "jira"),
    ("/jira-cloud-administration/", "jira"),
    ("/cloud/confluence", "confluence"),
    ("/cloud/jira/service-desk", "jsm"),
    ("/cloud/jira/platform", "jira"),
)

_CATEGORY_BY_SEGMENT: dict[str, str] = {
    "docs": "user-documentation",
    "administration": "administration",
    "rest": "rest-api",
    "v3": "rest-api",
    "platform": "developer",
    "service-desk": "developer",
}

_STOPWORDS = frozenset(
    "the a an and or of to in on for with is are was were by at from as it its this that be"
    " you your we our they their".split()
)


@dataclass(frozen=True, slots=True)
class PageMetadata:
    title: str
    title_confidence: float
    product: str
    product_confidence: float
    category: str
    category_confidence: float
    page_type: str
    page_type_confidence: float
    language: str
    language_confidence: float
    canonical_url: str


def _product_from_url(url: str) -> tuple[str, float]:
    path = urlsplit(url).path.lower()
    for segment, product in _PRODUCT_BY_PATH:
        if segment in path:
            # Authoritative when it is the publisher's own section path.
            return product, HIGH
    # Developer hosts that do not carry a product sub-section in the path:
    # fall back to the registered host identity; low confidence on a root
    # path because a root page is genuinely ambiguous.
    netloc = urlsplit(url).netloc.lower()
    if netloc == "support.atlassian.com":
        return "jira", LOW
    if netloc == "developer.atlassian.com":
        return "jira", LOW
    return "unknown", LOW


def _category_from_url(url: str) -> tuple[str, float]:
    parts = [p for p in urlsplit(url).path.lower().split("/") if p]
    if not parts:
        return "root", LOW
    if "rest" in parts:
        return "rest-api", HIGH
    for part in parts:
        if part in _CATEGORY_BY_SEGMENT:
            return _CATEGORY_BY_SEGMENT[part], MEDIUM
    return "general", LOW


def _page_type(title: str, url: str) -> tuple[str, float]:
    path = urlsplit(url).path.lower()
    if "/rest/" in path or "/api/" in path:
        return "api_reference", HIGH
    title_lower = title.lower()
    if any(title_lower.startswith(prefix) for prefix in ("how to", "create", "add", "delete")):
        return "how_to", MEDIUM
    if "what" in title_lower or "about" in title_lower:
        return "concept", MEDIUM
    return "guide", LOW


def _language(text: str) -> tuple[str, float]:
    words = [w for w in re.findall(r"[A-Za-z]+", text) if len(w) > 2]
    if not words:
        return "unknown", LOW
    ascii_letters = sum(1 for c in text if ord(c) < 128 and c.isalpha())
    letters = sum(1 for c in text if c.isalpha())
    ascii_ratio = ascii_letters / letters if letters else 0.0
    sample = words[:300]
    hits = sum(1 for w in sample if w.lower() in _STOPWORDS)
    stopword_ratio = hits / len(sample)
    if ascii_ratio > 0.9 and stopword_ratio >= 0.08:
        return "en", HIGH
    if ascii_ratio > 0.7 and stopword_ratio >= 0.03:
        return "en", MEDIUM
    return "unknown", LOW


def _canonical(html: str, url: str) -> str:
    match = re.search(r'<link\s+[^>]*rel=["\'][^>]*canonical[^>]*href=["\']([^"\']+)', html, re.I)
    if match:
        return match.group(1).strip()
    return url


def extract_metadata(html: str, *, url: str, extracted_text: str = "") -> PageMetadata:
    """Derive and return the five judgement fields plus canonical link.

    `extracted_text` is preferred for language detection when available,
    because the raw HTML dilutes stopword sampling with CSS/JS. When the
    caller has not run extraction yet, fall back to stripped page text.
    """
    title, title_conf = _title(html, url)
    product, product_conf = _product_from_url(url)
    category, category_conf = _category_from_url(url)
    page_type, page_type_conf = _page_type(title, url)
    text = extracted_text or re.sub(r"<[^>]+>", " ", html)
    language, language_conf = _language(text)
    return PageMetadata(
        title=title,
        title_confidence=title_conf,
        product=product,
        product_confidence=product_conf,
        category=category,
        category_confidence=category_conf,
        page_type=page_type,
        page_type_confidence=page_type_conf,
        language=language,
        language_confidence=language_conf,
        canonical_url=_canonical(html, url),
    )


def _title(html: str, url: str) -> tuple[str, float]:
    # Authoritative: an <h1> in the main article. High confidence: the
    # article named itself. Fallback: <title>. Lowest: the URL slug.
    h1 = re.search(r"<h1\b[^>]*>(.*?)</h1>", html, re.S | re.I)
    if h1:
        value = re.sub(r"<[^>]+>", "", h1.group(1)).strip()
        if value:
            return " ".join(value.split()), HIGH
    t = re.search(r"<title>(.*?)</title>", html, re.S | re.I)
    if t:
        value = t.group(1).strip()
        if value:
            return " ".join(value.split()), MEDIUM
    slug = urlsplit(url).path.rstrip("/").split("/")[-1].replace("-", " ")
    return slug or "Untitled", LOW


__all__ = [
    "PageMetadata",
    "extract_metadata",
    "HIGH",
    "MEDIUM",
    "LOW",
]
