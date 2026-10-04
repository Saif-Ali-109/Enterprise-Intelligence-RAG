"""Unit-test fixtures.

The one thing this file does is seed the placeholder credentials, and the fact
that it is a separate file from the root `conftest.py` is the point: a
session-scoped autouse fixture at the root would also apply to
`tests/integration/`, whose skip guards ask whether a *real* credential is
available. A placeholder injected by the same process answers that question
wrongly. See `tests/fixtures/credentials.py` for the full account.

A real key in the developer's environment is left alone — `setdefault`, not
assignment — so running the test suite can never spend a vendor's money by
accident.
"""

from __future__ import annotations

import pytest

from tests.fixtures.credentials import seed_placeholder_credentials


@pytest.fixture(scope="session", autouse=True)
def _dummy_credentials() -> None:
    """Seed the two required keys when the environment has not supplied them.

    `Settings` refuses to construct without `GROQ_API_KEY` and
    `PINECONE_API_KEY`. That is correct behaviour for the application and
    inconvenient for a test that makes no network call, which is every test in
    this directory. Session-scoped because `get_settings` is a module-level
    singleton: a per-test override would not take effect.
    """
    seed_placeholder_credentials()
