"""The credential guard that decides whether a live test runs.

This is the check that keeps `tests/integration/` honest, and it is tested here
rather than trusted, because the failure it prevents is invisible by
construction: a guard that is defeated produces live tests that run against fake
credentials and report results that look like vendor faults.

The bug this guards against actually happened. A session-scoped autouse fixture
in the root `conftest.py` seeded placeholder keys, so every integration test
found `PINECONE_API_KEY` present and ran. The output was a `401`, a
`ScopeMismatch` during fixture setup, and a `ProviderError` about a reasoning
channel that no service had produced — all of it indistinguishable from a real
problem with Pinecone or Groq, and all of it caused by the test suite lying to
itself about the environment.
"""

from __future__ import annotations

import pytest

from tests.fixtures.credentials import (
    PLACEHOLDER_CREDENTIALS,
    has_credential,
    seed_placeholder_credentials,
)


class TestPresenceIsNotEvidence:
    """The distinction the whole module exists to draw."""

    def test_a_placeholder_is_not_a_credential(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The exact bug, reduced to one line.

        `os.environ` contains the key. The key is worthless. A presence check
        says yes and the live test runs.
        """
        monkeypatch.setenv("PINECONE_API_KEY", "pcsk_testkey_not_a_real_key_0000000000")

        assert has_credential("PINECONE_API_KEY") is False

    def test_a_real_looking_key_passes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PINECONE_API_KEY", "pcsk_51abc…not-the-placeholder")

        assert has_credential("PINECONE_API_KEY") is True

    def test_an_unset_key_does_not_pass(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("PINECONE_API_KEY", raising=False)

        assert has_credential("PINECONE_API_KEY") is False

    @pytest.mark.parametrize("value", ["", "   ", "\n", "\t "])
    def test_whitespace_is_not_a_credential(
        self, monkeypatch: pytest.MonkeyPatch, value: str
    ) -> None:
        """A key that is present but blank fails, having been stripped.

        Without the strip, `os.environ.get(...)` returns a truthy string and the
        guard passes on a variable that was exported empty — which is a routine
        way to end up with an unset key in a CI job.
        """
        monkeypatch.setenv("GROQ_API_KEY", value)

        assert has_credential("GROQ_API_KEY") is False

    def test_a_surrounded_key_passes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Stripping must not turn a real key into a failure.

        A trailing newline is the usual culprit — `export KEY=$(cat key.txt)`
        keeps it — and a guard that rejected that would push a developer towards
        weakening the check entirely.
        """
        monkeypatch.setenv("GROQ_API_KEY", "  gsk_real_key_1234  ")

        assert has_credential("GROQ_API_KEY") is True


class TestSeeding:
    def test_seeding_never_overwrites_a_real_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """`setdefault`, not assignment.

        A test suite that overwrote real credentials would be a test suite that
        could quietly disable itself on a machine where the keys matter — and,
        worse, could make a unit test run against a real vendor account.
        """
        monkeypatch.setenv("PINECONE_API_KEY", "pcsk_genuine")

        seed_placeholder_credentials()

        import os

        assert os.environ["PINECONE_API_KEY"] == "pcsk_genuine"

    def test_seeding_supplies_a_key_that_is_not_a_credential(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The two must not contradict each other.

        If seeding produced a value `has_credential` accepted, every unit test
        would pass while the integration suite believed it had a live account.
        Asserting the pairing is what stops that from being introduced as a
        convenience.
        """
        for name in ("GROQ_API_KEY", "PINECONE_API_KEY"):
            monkeypatch.delenv(name, raising=False)

        seed_placeholder_credentials()

        import os

        for name in ("GROQ_API_KEY", "PINECONE_API_KEY"):
            assert os.environ[name] in PLACEHOLDER_CREDENTIALS
            assert has_credential(name) is False

    def test_every_placeholder_is_rejected_by_the_guard(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The rejection set and the seeding set are the same set.

        Written as a property over `PLACEHOLDER_CREDENTIALS` rather than as two
        lists, so a placeholder added to one and not the other fails here
        instead of producing a live test with a credential the guard trusts.
        """
        assert PLACEHOLDER_CREDENTIALS, "the placeholder set is empty; seeding has no guard"

        for value in PLACEHOLDER_CREDENTIALS:
            monkeypatch.setenv("PINECONE_API_KEY", value)
            assert has_credential("PINECONE_API_KEY") is False, (
                f"{value!r} is seeded as a placeholder but accepted as a credential"
            )
