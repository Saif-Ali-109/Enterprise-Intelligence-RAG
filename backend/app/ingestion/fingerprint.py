"""Content and unit fingerprints.

T044/T060. FR-026.

A fingerprint is how the crawler decides whether a page it is about to
index is *the same* as what it already has: same content, new fetch, skip
it; changed content, supersede the old vectors. It is the cheaper, quieter
version of "crawl again" — the outer guard that makes SC-012 (no duplicate
content on repeated crawls) a property of the pipeline itself, not of
operator memory.

Normalisation is load-bearing, and it is the *only* transformation that
ever happens to text before hashing. Publisher reformatting is the bane of
both:

- trailing whitespace trims and line wraps differ between fetches of an
  identical page — `collapse whitespace to one space` removes that noise;
- HTML/Unicode: extraction already handled this upstream; anything deeper
  would be a semantic decision produced without evidence.

No lowercasing, no stemming, no stop-word removal. A change that silently
merges two distinct versions of a page into one fingerprint means the old
version is *never* replaced — which is worse than missing a skip. Normalise
only what `strip().split()` can do blind.
"""

from __future__ import annotations

import hashlib

#: Domain's type for the decision the fingerprint feeds. "unchanged" maps
#: to `skip the fetch`, "supersede" to `index the new version and retire
#: the old`. Both describe the *same* identity, never "different page" —
#: that is a different URL entirely, decided by the registry, not the hash.
UNCHANGED = "unchanged"
SUPERSEDE = "supersede"


def _normalise(text: str) -> str:
    return " ".join(text.split())


#: The chunking policy's contribution to the fingerprint, as a short string.
#:
#: **Why the chunker's settings belong in a content fingerprint (measured
#: 2026-10-09).** The fingerprint decides whether a page is re-indexed. It was
#: computed over the page's extracted text alone, which made it blind to every
#: chunking decision: adding a filter that removes navigation from the corpus
#: left all 132 existing documents `unchanged`, so a re-crawl skipped them and
#: the polluted units stayed in the vector index — the fix was in the code and
#: nothing happened. Measured on the crawl that exposed it: `unchanged: 42` on
#: every source, corpus unchanged, while a newly discovered page was indexed
#: under the new policy. A fingerprint that cannot see how the corpus is built
#: cannot tell an operator that the corpus needs rebuilding.
#:
#: The version tag covers the *logic* (`CHUNKING_POLICY_VERSION`), the settings
#: cover the *parameters*. Bumping either invalidates every document, which is
#: the intended consequence of changing how units are built: re-crawl, re-embed,
#: and the index matches the policy again.
CHUNKING_POLICY_VERSION = "1"


def chunking_signature() -> str:
    """A stable short string for the chunking policy currently in force."""
    from app.core.config import get_settings

    settings = get_settings()
    material = "|".join(
        str(value)
        for value in (
            CHUNKING_POLICY_VERSION,
            settings.chunk_target_min_tokens,
            settings.chunk_target_max_tokens,
            settings.chunk_overlap_tokens,
            settings.chunk_min_useful_tokens,
            settings.chunk_drop_navigation,
            settings.chunk_navigation_min_words,
            settings.chunk_navigation_max_words_per_line,
        )
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def content_fingerprint(text: str) -> str:
    """SHA-256 over whitespace-normalised main content and the chunking policy.

    The normalisation deliberately ignores line-ending and padding noise,
    so a page whose source merely re-flowed between fetches keys to the
    same fingerprint.

    The chunking signature is mixed in so that changing how units are built
    invalidates every document — see `CHUNKING_POLICY_VERSION` for the measured
    case in which not doing so left a fixed pipeline serving the old index.
    """
    material = f"{chunking_signature()}\x00{_normalise(text)}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def unit_text_fingerprint(unit_text: str) -> str:
    """SHA-256 over a single unit's text, for the unit-level join key."""
    return hashlib.sha256(_normalise(unit_text).encode("utf-8")).hexdigest()


def classify_change(old_fingerprint: str | None, new_fingerprint: str) -> str:
    """Decide, from two fingerprints, whether content moved.

    `None` old fingerprint (never indexed / fingerprint unknown) means this
    *is* new content: it must be indexed, not skipped. A fingerprint equal
    to the last indexed one means the fetch changed nothing. Only a
    genuinely different hash supersedes.
    """
    if old_fingerprint is None:
        return SUPERSEDE
    if old_fingerprint == new_fingerprint:
        return UNCHANGED
    return SUPERSEDE


__all__ = [
    "CHUNKING_POLICY_VERSION",
    "chunking_signature",
    "content_fingerprint",
    "unit_text_fingerprint",
    "classify_change",
    "UNCHANGED",
    "SUPERSEDE",
]
