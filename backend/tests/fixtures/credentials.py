"""Which credentials are real, and how to tell.

This module exists because of a bug it prevents. The test suite seeds obviously
fake `GROQ_API_KEY` and `PINECONE_API_KEY` values so that `Settings` can
construct for tests that make no network call. That seeding was originally an
autouse *session* fixture in the root `conftest.py`, which meant it also applied
to `tests/integration/` — and the integration modules guard themselves on
`os.environ.get("PINECONE_API_KEY")` being present. So the guard was satisfied by
the placeholder, every live test ran against a fake credential, and the results
were failures that looked exactly like vendor faults: a `401`, a `ScopeMismatch`
at fixture setup, a `ProviderError` about a reasoning channel that no real
service had produced.

The lesson is narrow and worth stating: **a presence check cannot be satisfied
by a value the same process put there.** `PINECONE_API_KEY in os.environ` is a
statement about the environment, not about whether a credential is usable.

Two mechanisms, because one is not enough:

1. The placeholders live in `tests/unit/` and `tests/contract/` conftests, never
   at the root. `tests/integration/` therefore sees the environment the developer
   actually exported, and a missing key is genuinely missing.
2. `has_credential()` still rejects the placeholder, so a future refactor that
   re-hoists the seeding to the root fails loudly and legibly instead of
   quietly spending a vendor's time and reporting nonsense.
"""

from __future__ import annotations

import os

#: The values the test suite substitutes. Recognisable by design: a credential
#: that says it is not real cannot be mistaken for one that is, in a log, in a
#: failure message, or by a human reading a CI transcript.
PLACEHOLDER_CREDENTIALS = frozenset(
    {
        "gsk_testkey_not_a_real_key_0000000000",
        "pcsk_testkey_not_a_real_key_0000000000",
    }
)


def has_credential(name: str) -> bool:
    """Whether `name` is set to something that is not a test placeholder.

    Used by the integration suite's skip guards. The check is on the *value*,
    not on presence, because presence is not evidence: this process may have
    written the value itself.
    """
    value = os.environ.get(name, "").strip()
    return bool(value) and value not in PLACEHOLDER_CREDENTIALS


def seed_placeholder_credentials() -> None:
    """Install the placeholder keys, leaving any real one untouched.

    `setdefault`, not assignment. A developer with real keys in their
    environment must not have them overwritten by running the test suite, both
    because that would be a surprising side effect and because a unit test that
    silently spent a vendor's money is worse than one that fails.
    """
    os.environ.setdefault("GROQ_API_KEY", "gsk_testkey_not_a_real_key_0000000000")
    os.environ.setdefault("PINECONE_API_KEY", "pcsk_testkey_not_a_real_key_0000000000")
