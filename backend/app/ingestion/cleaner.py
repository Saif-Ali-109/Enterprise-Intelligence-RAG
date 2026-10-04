"""Stage 2 content-type validation and stage 4 boilerplate rejection.

T051. FR-029.

Two cheap judgements before a page ever reaches extraction or indexing: is
the response actually an HTML document (stage 2), and is what trafilatura
would extract worth indexing at all (stage 4).

Why this module exists separately from extraction: `extractor` answers "what
is the main content?", and a page that yields five words is an honest
answer to that question. `cleaner` answers "should we index it?" — a
different, earlier decision, and conflating the two is how a stub or an
error page becomes a searchable "document" and then pollutes retrieval
with one-sentence "main content" (FR-029's whole point is preventing that).

The boilerplate metric is deliberate about what it cannot know. We do not
have the publisher's templates; we can only measure repetition, size, and
objectively-recognisable error markers in the extracted text:

- **too small** — the text does not carry one substantive paragraph. Below
  the floor, a unit would fail the usefulness floor downstream anyway;
  rejecting at this boundary keeps the noise out of the chunker rather than
  out of the audit trail.
- **boilerplate-heavy** — the prose repeats itself. Unique-trigram ratio:
  a page that says the same navigation sentence twenty times has a vanishing
  trigram diversity; a normal page has most of its trigrams unique.
- **error page** — the page is a `404`, an access-restriction shell, or a
  maintenance notice. The served status should already have failed the
  fetch, but a `200` that *is* an error message must still not be indexed.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

#: Minimum extracted text (chars) for a page to be worth indexing. Below
#: this there is not enough text to form one meaningful unit; the floor
#: deliberately mirrors the chunker's usefulness floor so "rejected as
#: boilerplate" and "chunk dropped as unusable" cannot disagree about the
#: cutoff.
MIN_CONTENT_CHARS: int = 200

#: A page whose single most-repeated 4-gram accounts for more than this
#: share of all 4-grams is mostly restating itself: the template sentence,
#: not the content, is the document. A normal page spreads its 4-grams
#: across varied phrasing, so the leader holds a small share. The choice of
#: a *single-leader* metric is deliberate — uniqueness ratios collapse when
#: one long sentence merely repeats a section intro, and that is a page that
#: *says* the sentence twice, not a boilerplate page.
MAX_TOP_NGRAM_SHARE: float = 0.08

#: Content types this pipeline extracts HTML from. Everything else — PDFs,
#: JSON blobs, images — must be rejected *before* trafilatura is asked to
#: parse it, because trafilatura's output for those is not prose at all.
_HTML_TYPES: frozenset[str] = frozenset({"text/html", "application/xhtml+xml"})

_ERROR_MARKERS: tuple[str, ...] = (
    "page not found",
    "this page isn't available",
    "access denied",
    "forbidden",
    "we can't find",
    "404",
    "an error occurred",
    "temporarily unavailable",
)


class ContentRejected(Exception):
    """A page is honestly rejected, never indexed as noise.

    Carries the measured reason so the crawl job's audit trail can record
    "rejected as too small" instead of silently succeeding. Not an
    `AppError` subclass because a page-level judgement is not an API error;
    no client sends a request this rejects.
    """

    def __init__(self, reason: str, *, details: dict[str, object] | None = None) -> None:
        self.reason = reason
        self.details = details or {}
        super().__init__(reason)


def assert_html_content_type(content_type: str | None) -> None:
    """Stage 2: the body must be the kind of document extraction understands.

    `None` is rejected too. A server that does not say what it sent is
    exactly the case where guessing and proceeding is how a microdata JSON
    endpoint gets extracted as "prose".
    """
    if content_type is None:
        raise ContentRejected("missing_content_type")
    media = content_type.split(";", 1)[0].strip().lower()
    if media not in _HTML_TYPES:
        raise ContentRejected("non_html_content_type", details={"content_type": content_type})


def _trigrams(text: str) -> Counter[tuple[str, str, str]]:
    words = text.split()
    return Counter((words[i], words[i + 1], words[i + 2]) for i in range(len(words) - 2))


def _ngrams(text: str, n: int = 4) -> Counter[tuple[str, ...]]:
    words = text.split()
    return Counter(tuple(words[i : i + n]) for i in range(len(words) - n + 1))


def assess_boilerplate(text: str) -> None:
    """Stage 4: reject pages that are noise even when extraction succeeds.

    Raises `ContentRejected` with one of:
      - `"error_page"` — an error notice mapped to a 200 response;
      - `"too_small"` — below the usefulness floor, none of the detail
        worth citing;
      - `"boilerplate_heavy"` — the text largely repeats itself.
    """
    stripped = text.strip()
    head = stripped[:400].lower()
    if any(marker in head for marker in _ERROR_MARKERS):
        raise ContentRejected("error_page")

    if len(stripped) < MIN_CONTENT_CHARS:
        raise ContentRejected("too_small", details={"chars": len(stripped)})

    grams = _ngrams(stripped.lower(), 4)
    if grams:
        top_share = grams.most_common(1)[0][1] / sum(grams.values())
        if top_share >= MAX_TOP_NGRAM_SHARE:
            raise ContentRejected(
                "boilerplate_heavy",
                details={"top_4gram_share": round(top_share, 3)},
            )


@dataclass(frozen=True, slots=True)
class CleanerResult:
    """A cleaner verdict that did not raise; the extracted text stays."""

    text: str
    words: int

    @property
    def word_count(self) -> int:
        return self.words


def clean(text: str) -> CleanerResult:
    """Run stage 4 on already-extracted text; return it when it survives."""
    assess_boilerplate(text)
    return CleanerResult(text=text, words=len(text.split()))


__all__ = [
    "ContentRejected",
    "CleanerResult",
    "assess_boilerplate",
    "assert_html_content_type",
    "clean",
    "MIN_CONTENT_CHARS",
    "MAX_TOP_NGRAM_SHARE",
]
