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
