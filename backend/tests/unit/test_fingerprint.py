"""Content fingerprint and the change-detection decision.

T044. FR-026, SC-012. Repeated crawls must never accumulate duplicates:
a fingerprint that answers a change honestly means only changed content
supersedes, and nothing changeless ever replaces it.
"""

from __future__ import annotations

import pytest
from app.ingestion.fingerprint import (
    SUPERSEDE,
    UNCHANGED,
    classify_change,
    content_fingerprint,
    unit_text_fingerprint,
)

pytestmark = pytest.mark.unit


class TestStability:
    def test_same_text_same_fingerprint(self) -> None:
        text = "Create a space to organize your work."
        assert content_fingerprint(text) == content_fingerprint(text)

    def test_whitespace_reflow_same_fingerprint(self) -> None:
        a = "Create a space to  organize your work.\nSecond line."
        b = "Create a space to organize your work. Second line."
        assert content_fingerprint(a) == content_fingerprint(b)

    def test_changed_content_hashes_differently(self) -> None:
        a = "Create a space to organize your work."
        b = "Create a space to organize everyone together."
        assert content_fingerprint(a) != content_fingerprint(b)

    def test_case_change_supersedes(self) -> None:
        # Lowercasing is deliberately absent; changing case is a content change.
        a = "Create a Space."
        b = "create a space."
        assert content_fingerprint(a) != content_fingerprint(b)

    def test_unit_and_document_fingerprints_differ_by_text_only(self) -> None:
        unit = "A single unit's text."
        assert unit_text_fingerprint(unit) == unit_text_fingerprint("  A single unit's text.\n")


class TestChangeDecision:
    def test_first_seen_is_new_indexed(self) -> None:
        assert classify_change(None, "abc") == SUPERSEDE

    def test_same_fingerprint_skips(self) -> None:
        assert classify_change("abc", "abc") == UNCHANGED

    def test_changed_fingerprint_supersedes(self) -> None:
        assert classify_change("abc", "abd") == SUPERSEDE
