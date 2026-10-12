"""Test-suite-wide fixtures.

**Vendor fakes are registered here.** They live in `tests/fixtures/vendors.py`
rather than in this file so they can be imported and reused by the integration
suite, but pytest only discovers fixtures in `conftest.py` — a fixture defined
in a plain module is invisible, and the failure is `fixture 'fake_pinecone' not
found` on every test that asked for it. Re-exporting them here is the whole
reason this file exists.

**Credential placeholders are NOT seeded here, and their absence is deliberate.**
They are seeded by `tests/unit/conftest.py` and `tests/contract/conftest.py`
instead. A session-scoped autouse fixture at this level applies to
`tests/integration/` as well, and the integration modules decide whether to run
by asking whether a real credential is present — a question a placeholder
injected by the same process would answer wrongly, in the direction of making
live tests run against fake keys. The full account of how that happened is in
`tests/fixtures/credentials.py`; the short version is that a presence check
cannot be satisfied by a value the same process put there.

Note what is *not* here: no database fixture, no vendor account, no network. A
test that needs one of those belongs in `tests/integration/` and says so in its
own module docstring. Putting a live dependency in a `conftest.py` makes it
invisible which tests actually need it, and the answer to "why did the unit suite
just try to reach Pinecone" is always that.

**The one exception is a routing decision, not a fixture.** `pytest_configure`
below repoints `DATABASE_URL` at a disposable database for the suites that
truncate, because the DSN has to be right before the settings singleton is read
at import time — which is earlier than any fixture runs. It creates no session,
opens no connection on behalf of a passing test, and needs no database at all when
PostgreSQL is not running.
"""

from __future__ import annotations

import pytest

from tests.fixtures.vendors import (  # noqa: F401 - re-exported as fixtures
    FakeIndexes,
    FakeInference,
    FakePineconeClient,
    FakePineconeIndex,
    fake_index,
    fake_pinecone,
    make_empty_search_response,
    make_index_model,
    make_rerank_result,
    make_search_response,
)


def pytest_configure(config: pytest.Config) -> None:
    """Point the whole suite at a database it may destroy.

    The integration, e2e **and contract** suites truncate nine tables between
    tests. Until this hook existed they ran against the development database, so
    `make test-all` erased a crawled corpus — which cost one measured evaluation
    run, taken again from an empty index (see `tests/fixtures/database.py`).

    **Every selection is redirected, not just the destructive ones.** The first
    version redirected only when `-m` mentioned `integration` or `e2e`, on the
    reasoning that those were the suites that truncate. They were not the only
    ones: `tests/contract/test_sse_termination.py` and `test_no_reasoning.py`
    truncate too, and `make check` runs `-m "unit or contract"`. That gap was
    found by the guard in `truncate_all` refusing to run against the development
    database during `make check` — the refusal is the check working, and it is why
    this reads as a design note rather than a post-mortem of a truncated corpus.

    The decision must be made before anything reads the settings singleton, and
    settings are read at import time, which is why it is a hook and not a fixture.
    An unreachable PostgreSQL is not an error here: the redirect is applied
    regardless, and each module's own connection check turns the missing database
    into a skip — so a pure unit run still works with no database at all.

    `TEST_DATABASE_URL` overrides the derived name for CI.
    """
    from tests.fixtures.database import prepare_test_database

    try:
        dsn = prepare_test_database()
    except Exception as exc:  # noqa: BLE001 - a skip is the intended outcome
        # Deliberately not a hard failure: PostgreSQL being down must not stop a
        # suite that does not need it from running. Each database-dependent module
        # turns the unreachable DSN into its own skip.
        print(
            f"\n  database redirect unavailable ({type(exc).__name__}); "
            "database-dependent tests will skip\n"
        )
        return

    from tests.fixtures.database import _redact

    print(f"\n  test database: {_redact(dsn)}\n")


@pytest.fixture(autouse=True)
def _reset_module_singletons() -> None:
    """Clear the cached vendor clients and health probes around every test.

    `get_vector_store()`, `get_reranker()` and `get_language_model()` are
    process-wide singletons. A test that registers a fake client would otherwise
    leave it in place, and the next test would silently use the previous test's
    double — which passes far more often than it should and is close to
    impossible to debug from a failure message.
    """
    from app.api.routes_health import reset_probes
    from app.generation.provider import reset_language_model
    from app.retrieval.reranker import reset_reranker
    from app.retrieval.vector_store import reset_vector_store

    reset_probes()
    reset_vector_store()
    reset_reranker()
    reset_language_model()

    yield

    reset_probes()
    reset_vector_store()
    reset_reranker()
    reset_language_model()


__all__ = [
    "FakeIndexes",
    "FakeInference",
    "FakePineconeClient",
    "FakePineconeIndex",
    "fake_index",
    "fake_pinecone",
    "make_empty_search_response",
    "make_index_model",
    "make_rerank_result",
    "make_search_response",
]
