"""Contract-test fixtures.

Mirrors `tests/unit/conftest.py`: the placeholder credentials are seeded here so
that `create_app()` and `get_settings()` can construct, and *not* at the root,
where an autouse session fixture would also reach `tests/integration/` and defeat
the skip guards that ask whether a real credential is present.

The contract tests build the app and read its generated OpenAPI document. They
make no network call, so a placeholder is sufficient and appropriate.
"""

from __future__ import annotations

import pytest

from tests.fixtures.credentials import seed_placeholder_credentials


@pytest.fixture(scope="session", autouse=True)
def _dummy_credentials() -> None:
    """Seed the two required keys when the environment has not supplied them."""
    seed_placeholder_credentials()
