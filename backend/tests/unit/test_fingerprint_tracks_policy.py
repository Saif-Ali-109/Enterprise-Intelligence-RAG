"""The fingerprint must see how the corpus is built (measured 2026-10-09).

The fingerprint is the decision behind "unchanged", which skips re-indexing a
page. Computed over the page's text alone it was blind to every chunking
decision, and the consequence was measured on a live crawl: after a filter was
added that removes navigation from the corpus, a re-crawl of every source
reported `unchanged` on all of them and the corpus did not move — while a newly
discovered page was indexed under the new policy. The fix was in the code and
the running system served the old index, which is the worst way for a fix to be
invisible.
"""

from __future__ import annotations

import pytest
from app.core.config import Settings
from app.ingestion.fingerprint import (
    CHUNKING_POLICY_VERSION,
    chunking_signature,
    content_fingerprint,
)

from tests.fixtures.database import is_test_database

pytestmark = pytest.mark.unit

PAGE = (
    "Open the filter manager and choose Save as filter. A saved filter can be "
    "run again by name from the same screen."
)


def _settings_with(**overrides: object) -> Settings:
    from app.core.config import get_settings

    return get_settings().model_copy(update=overrides)


def test_the_signature_is_stable_within_one_policy() -> None:
    assert chunking_signature() == chunking_signature()


def test_the_signature_names_its_version() -> None:
    """A version tag is what a code change bumps; the settings alone cannot say it."""
    assert CHUNKING_POLICY_VERSION == "1"


@pytest.mark.parametrize(
    "override",
    [
        {"chunk_target_min_tokens": 601},
        {"chunk_target_max_tokens": 1001},
        {"chunk_overlap_tokens": 101},
        {"chunk_min_useful_tokens": 51},
        {"chunk_drop_navigation": False},
        {"chunk_navigation_min_words": 13},
        {"chunk_navigation_max_words_per_line": 6.5},
    ],
)
def test_every_chunking_setting_changes_the_fingerprint(
    monkeypatch: pytest.MonkeyPatch, override: dict[str, object]
) -> None:
    """Each one, because each one changes the units a page produces.

    A parameter that alters the corpus must invalidate the corpus. A parameter
    that changes nothing about the units has no business being here — that is why
    this is a named list rather than "whatever settings happen to exist".
    """
    from app.core import config as config_module

    original = config_module.get_settings
    before = chunking_signature()

    monkeypatch.setattr(
        config_module, "get_settings", lambda: original().model_copy(update=override)
    )

    assert chunking_signature() != before, f"{override} did not change the signature"


def test_identical_text_still_fingerprints_identically() -> None:
    """The normalisation guarantee this file must not break.

    Two fetches of the same page differing only in line endings must remain one
    page, or every crawl re-embeds the entire corpus.
    """
    assert content_fingerprint("a  b\nc") == content_fingerprint("a b c")


def test_different_text_still_fingerprints_differently() -> None:
    assert content_fingerprint(PAGE) != content_fingerprint(PAGE + " And more.")


def test_the_fingerprint_is_not_a_bare_hash_of_the_text() -> None:
    """Guards the regression this module exists to prevent.

    If someone simplifies `content_fingerprint` back to hashing the text alone,
    this fails — and the failure is the one thing that cannot be discovered by a
    unit test later, because the symptom is a corpus that silently stops
    tracking the policy that built it.
    """
    import hashlib

    bare = hashlib.sha256(" ".join(PAGE.split()).encode("utf-8")).hexdigest()
    assert content_fingerprint(PAGE) != bare


def test_the_guard_only_applies_to_a_test_database() -> None:
    """Sanity check on the harness itself, and the corpus-isolation rule.

    This module imports nothing that connects, so the assertion holds wherever it
    runs — but it is worth asserting that the process is pointed at a disposable
    database when the wider suite runs, because a chunking change that lands
    without this guard would be invisible rather than merely wrong.
    """
    assert is_test_database() or "DATABASE_URL" not in __import__("os").environ
