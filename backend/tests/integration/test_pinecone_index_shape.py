"""Live Pinecone verification: the index shape the pipeline actually depends on.

T027. **Executed 2026-09-30 against a live serverless account: 13 passed.** The
measurements that run produced are recorded in `research.md` R-002 and R-001.
It requires a real `PINECONE_API_KEY` and skips with an explicit message when one
is absent, so that number is a property of that run and not of this file.

The distinction this file exists to protect: `tests/unit/test_vendor_adapters.py`
verifies that the adapter reads the SDK's response types correctly, using
responses built from the real SDK classes. It cannot verify that the *service*
behaves as documented. This file is the half that needs the network, and the two
are not substitutes for each other.

**The first execution found four defects in the write path, all of which had
passed the unit tests** — and one of those four had a written research note
against it already. The write path was wrong in three independent ways at once:

| Sent | Result |
|---|---|
| `upsert(vectors=[{"values": [0.0], ...}])` | `[400] Vector dimension 1 does not match the dimension of the index 1024` |
| `upsert_records(records=[{"metadata": {...}}])` | `[400] Invalid type for field 'metadata' — must be a string, number, boolean or list of strings` |
| `upsert_records(records=[{"id": ...}])` | accepted, but the service names records by `_id` |
| `upsert_records(records=[{"_id": ..., <fields at top level>}])` | **accepted** |

R-003 had already recorded the correct form — "write with `upsert_records` (text +
metadata, no `values`)" — and the code did not follow it. A written decision
prevented nothing, because nothing checked the code against the decision. The
unit test for the write path asserted `values == [0.0]` as correct, which is the
clearer lesson: the test did not merely miss the bug, it enshrined it.

## What the live call answered

**1. `input_type` is persisted and readable.** The index reports
`read_parameters: {input_type: 'query', truncate: 'END'}` and
`write_parameters: {input_type: 'passage', truncate: 'END'}`, so
`IndexInfo.input_type_mismatch()` is checking a real field rather than returning
`None` because the field is absent.

**2. `fields=[]` returns no record data.** Confirmed: a search with `fields=[]`
returns `{'id_': ..., 'score_': ..., 'fields': {}}`. The registry stays the
authoritative copy of a unit's text, and the one request path that runs per
question does not egress the corpus.

**3. An over-length chunk is embedded in full — the ceiling is bytes, not tokens.**
A marker placed only in the final bytes of a 19,515-byte record is still findable,
and this holds up to 38,350 bytes (~7,800 estimated tokens), far past the
2,048-token model limit. R-001's silent-truncation risk does not materialise.

**4. The real limit is 40,960 bytes per vector**, and the record's text counts
against it: `Invalid record: Metadata size is 76009 bytes, which exceeds the limit
of 40960 bytes per vector`. A chunk above that is **rejected**, not truncated —
the better of the two outcomes, since it fails loudly, but a hard failure mode
that `embed_hard_token_limit` alone does not cover.

**5. The namespace round-trips**, exercising `document_id#ordinal` end to end.

## Running these

Requires a Pinecone **serverless** project and a key exported as
`PINECONE_API_KEY`. `ensure_index` creates the index if it is absent, so no
manual setup is needed beyond the project itself. The suite is safe to run
repeatedly; it does not delete the index and only removes the records it wrote.

Note that `PINECONE_API_KEY` must be in the *environment*, not only in `.env`.
The skip guard reads `os.environ` on purpose, because the suite writes its own
placeholder values and a file-based check would be satisfied by one of those.
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from app.core.errors import ProviderError, VectorServiceUnavailable

from tests.fixtures.credentials import has_credential

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not has_credential("PINECONE_API_KEY"),
        reason=(
            "T027 requires a live Pinecone account. No real PINECONE_API_KEY is set — "
            "a placeholder does not count. Executed 2026-09-30: 13 passed, "
            "having found four defects in the write path. A skip now is not a "
            "pass, and is not the same as that run."
        ),
    ),
]


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def live_store():
    """A live `PineconeVectorStore` against the configured index.

    Module-scoped because `ensure_index` is a billable, slow call and the tests
    below only read. It creates the index if it does not exist, which is the
    documented startup path rather than a test-only shortcut.

    `loop_scope="module"` is load-bearing. `pytest.ini` defaults fixtures to
    `asyncio_default_fixture_loop_scope = function`, and a module-scoped
    `async def` fixture that does not override it fails at setup with a
    `ScopeMismatch` — inside a `try` that skips, so the module would have
    reported "Pinecone is not reachable" for a configuration problem and sent
    an operator to debug the wrong layer.
    """
    from app.retrieval.vector_store import ensure_index

    try:
        info = await ensure_index()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(
            f"Pinecone is not reachable with the configured key: {type(exc).__name__}: {exc}"
        )

    assert info.ready, f"index {info.name!r} is not ready: {info.state}"

    from app.retrieval.vector_store import get_vector_store

    store = get_vector_store()
    try:
        yield store
    finally:
        from app.retrieval.vector_store import reset_vector_store

        reset_vector_store()


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def live_reranker():
    """A live `PineconeReranker`, guarded independently of `live_store`.

    The reranker calls Pinecone's *inference* endpoint while `live_store` calls
    the index service. Same key, same project, different services — so a
    reachability check on one says nothing about the other, and the two need
    separate guards. Without this, a project provisioned for indexes but not for
    inference would fail the rerank test as a hard error rather than reporting
    the capability it is missing.

    The probe is a one-document rerank: the cheapest call that still proves the
    endpoint exists, accepts the model, and returns a score.
    """
    from app.retrieval.reranker import PineconeReranker, RerankCandidate

    probe = RerankCandidate(
        id="probe-0",
        text="Jira Service Management request queues accept a customer portal.",
        retrieval_score=0.5,
    )

    try:
        await PineconeReranker().rerank(query="probe", candidates=[probe], top_n=1)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(
            f"Pinecone inference is not reachable with the configured key: {type(exc).__name__}: {exc}"
        )

    yield PineconeReranker()

    from app.retrieval.reranker import reset_reranker

    reset_reranker()


# ============================================================================
# The index shape
# ============================================================================


class TestLiveIndexShape:
    async def test_the_index_exists_and_is_ready(self, live_store) -> None:
        info = await live_store.describe()

        assert info.ready is True
        assert info.name == live_store.index_name
        assert info.state

    async def test_the_index_uses_integrated_embedding(self, live_store) -> None:
        """The service derives the dimension; this application does not.

        If this index is a `dense_vector` index, the whole adapter is on the
        wrong path and every other test in this file is measuring the wrong
        thing. So it is asserted first and in isolation.
        """
        info = await live_store.describe()

        assert info.read_input_type is not None, (
            "The service returned no read input_type. Either the index is not an "
            "integrated-embedding index, or `SemanticTextField.read_parameters` "
            "is not where the setting is stored — in which case "
            "`IndexInfo.input_type_mismatch()` returns None for the wrong reason "
            "and the R-001 check has been silently inert."
        )

    async def test_input_type_is_the_documented_asymmetry(self, live_store) -> None:
        """`query` for reads, `passage` for writes (R-001).

        This is the assertion that matters most. A symmetric configuration
        degrades search quality while every call still succeeds, so nothing else
        in the system would ever report it.
        """
        info = await live_store.describe()

        assert info.read_input_type == "query", (
            f"reads embed as {info.read_input_type!r}; expected 'query'. "
            "input_type is fixed at creation, so the index must be recreated."
        )
        assert info.write_input_type == "passage", (
            f"writes embed as {info.write_input_type!r}; expected 'passage'. "
            "input_type is fixed at creation, so the index must be recreated."
        )
        assert info.input_type_mismatch() is None

    async def test_the_embedding_model_is_the_configured_one(self, live_store) -> None:
        """The model cannot be changed after creation either."""
        info = await live_store.describe()

        assert info.embed_model == live_store.embed_model, (
            f"index is embedded with {info.embed_model!r}, configured for "
            f"{live_store.embed_model!r}"
        )

    async def test_no_dimension_is_hard_coded(self, live_store) -> None:
        """Principle VII: the service owns dimensionality.

        `expected_dimension` is `None` and there is nothing to compare against,
        which is the point — a hard-coded expected value would be a claim this
        application has no way to verify.
        """
        info = await live_store.describe()

        assert info.expected_dimension is None
        # A `semantic_text` field has no dimension of its own. If this ever
        # becomes an int, the index stopped being integrated-embedding.
        assert info.dimension is None


# ============================================================================
# The write path
# ============================================================================


class TestLiveRoundTrip:
    async def test_upsert_then_search_finds_the_record(self, live_store) -> None:
        """The `document_id#ordinal` id convention, end to end.

        Written to a per-run namespace and removed afterwards, so a run does not
        pollute the corpus a later run searches. The id shape matters because
        the citation validator parses the ordinal back out of it.
        """
        namespace = f"t027-smoke-{uuid.uuid4().hex[:8]}"
        record_id = f"{uuid.uuid4()}#0"
        text = "Jira Service Management request queues accept a customer portal."

        try:
            written = await live_store.upsert(
                [_vector_record(record_id, text)],
                namespace=namespace,
            )
            assert written.written == 1, (
                f"upsert acknowledged {written.written} of {written.submitted}"
            )

            hits = await _poll_for(live_store, namespace, record_id)

            assert hits, "the record was written but a search for it returned nothing"
            assert hits[0].id == record_id
            assert 0.0 <= hits[0].score <= 1.0
            assert hits[0].metadata["namespace"] == namespace
        finally:
            await _drop_namespace(live_store, namespace)

    async def test_search_returns_no_record_fields(self, live_store) -> None:
        """`fields=[]` must mean "no data", not "all data".

        The adapter sends an empty field list so the registry stays the
        authoritative copy of a unit's content. If the service treats the empty
        list as an omitted argument, every query returns the full chunk text and
        the answer path receives a second, unauthoritative copy of third-party
        content — the transfer is silent, so only this assertion detects it.
        """
        namespace = f"t027-smoke-{uuid.uuid4().hex[:8]}"
        marker = f"t027-marker-{uuid.uuid4().hex}"
        record_id = f"{uuid.uuid4()}#0"

        await live_store.upsert(
            [
                _vector_record(
                    record_id, f"Marker text {marker} about Atlassian product configuration."
                )
            ],
            namespace=namespace,
        )

        try:
            hits = await _poll_for(live_store, namespace, record_id)
            assert hits, "the record was written but a search for it returned nothing"

            rendered = repr([hit.metadata for hit in hits])
            assert marker not in rendered, (
                "the search response carried the record's text even though the "
                "request asked for no fields. The corpus is being transferred on "
                "every query."
            )
        finally:
            await _drop_namespace(live_store, namespace)

    async def test_upsert_reports_what_the_service_acknowledged(self, live_store) -> None:
        """A short write must be visible, not swallowed.

        The adapter reports `submitted` and `written` separately precisely so an
        unanswered question can be traced to missing chunks rather than to a
        retrieval failure.
        """
        namespace = f"t027-smoke-{uuid.uuid4().hex[:8]}"
        records = [
            _vector_record(f"{uuid.uuid4()}#{i}", f"Chunk {i} about indexing.") for i in range(3)
        ]

        result = await live_store.upsert(records, namespace=namespace)

        assert result.submitted == 3
        assert result.written == 3, f"service acknowledged {result.written} of 3 records"

        await _drop_namespace(live_store, namespace)

    async def test_delete_removes_only_the_named_records(self, live_store) -> None:
        """FR-054 and FR-056 both depend on this.

        A delete that removes too much is a data-loss bug; one that removes too
        little leaves deleted content queryable, which is the specific failure
        the tombstone design exists to prevent.
        """
        namespace = f"t027-smoke-{uuid.uuid4().hex[:8]}"
        keep = f"{uuid.uuid4()}#0"
        remove = f"{uuid.uuid4()}#0"

        await live_store.upsert(
            [
                _vector_record(keep, "Retained chunk about Atlassian configuration."),
                _vector_record(remove, "Removable chunk about Atlassian configuration."),
            ],
            namespace=namespace,
        )
        assert await _poll_for(live_store, namespace, keep)

        assert await live_store.delete([remove], namespace=namespace) == 1

        # Wait for the delete to propagate before asserting the survivor is
        # still there, so a slow delete is not read as a failed survivor check.
        remaining = await _poll_until(
            lambda: _search_ids(live_store, namespace), lambda ids: remove not in ids
        )
        assert remove not in remaining
        assert keep in remaining, "deleting one record removed another"

        await _drop_namespace(live_store, namespace)

    async def test_an_empty_delete_is_refused(self, live_store) -> None:
        """`delete([])` is a namespace wipe at the service, not a no-op.

        Asserted against the live service rather than only the fake, because
        this is the one place where the guard's value depends on the service's
        actual behaviour rather than on the SDK's.
        """
        with pytest.raises(ValueError, match="wipe the namespace"):
            await live_store.delete([])


# ============================================================================
# The unresolved question from R-002
# ============================================================================


class TestChunkLengthCeiling:
    async def test_a_chunk_within_the_byte_limit_is_embedded_in_full(self, live_store) -> None:
        """The answer, now measured: nothing is truncated; bytes are the limit.

        This test used to assert nothing, because the question was open. R-001's
        concern was that `llama-text-embed-v2` has a 2,048-token ceiling and an
        over-length chunk might be silently truncated, so a chunk would be
        embedded on its prefix while its citation claimed the whole thing.

        **It is not truncated.** Measured against this index: a marker placed
        *only in the last 20 bytes* of a record is still findable by a search for
        that marker, at every size from 2,950 bytes to 38,350 bytes (~600 to
        ~7,800 estimated tokens — far past the 2,048-token model limit). The
        service embeds the whole string.

        The real ceiling is a **byte** limit, not a token one:

        ```
        Invalid record: Metadata size is 76009 bytes, which exceeds
        the limit of 40960 bytes per vector
        ```

        40,960 bytes per vector, and the record's text counts against it. The
        chunker's 600–1000-token target sits comfortably inside that (roughly
        2,500–4,200 bytes), so R-001's mitigation is not load-bearing for
        truncation reasons — but the byte limit is a **new** hard failure mode
        that can reject an otherwise valid write, and it is why
        `embed_hard_token_limit` cannot be the only ceiling anyone checks.

        A distractor record of identical size is written alongside the target, so
        a match cannot be carried by similarity to the shared head.
        """
        namespace = f"t027-smoke-{uuid.uuid4().hex[:8]}"
        head = "Jira Service Management request queues accept a customer portal. " * 300
        record_id = f"{uuid.uuid4()}#0"
        distractor = f"{uuid.uuid4()}#0"

        result = await live_store.upsert(
            [
                _vector_record(record_id, f"{head} ZEBRAFISHTOKEN"),
                _vector_record(distractor, head),
            ],
            namespace=namespace,
        )
        assert (result.written, result.submitted) == (2, 2), (
            f"the service acknowledged {result.written}/{result.submitted}"
        )

        # The decisive check: a token that appears nowhere but the final bytes.
        hits = await _poll_for(
            live_store, namespace, record_id, query="ZEBRAFISHTOKEN", expect=record_id
        )
        assert record_id in {hit.id for hit in hits}, (
            "a marker in the last bytes of the record is not findable, so the "
            "service embedded a truncated prefix. The chunker's ceiling would "
            "then be load-bearing and citations would claim more text than was "
            "indexed."
        )
        print(
            f"\n  T027 tail marker in a {len(head) + 15:,}-byte record: FOUND "
            "(service embeds the full text; the ceiling is bytes, not tokens)"
        )

        await _drop_namespace(live_store, namespace)

    async def test_a_chunk_past_the_byte_limit_is_rejected(self, live_store) -> None:
        """The other half, and the reason the one above is not the whole story.

        Above 40,960 bytes the write is **rejected**, not truncated. So a chunk
        that is too long produces a hard error and a gap in the corpus rather
        than a quietly incomplete embedding — which is the better of the two
        outcomes, and means ingestion has to fail loudly rather than assume the
        service will cope.
        """
        namespace = f"t027-smoke-{uuid.uuid4().hex[:8]}"
        record_id = f"{uuid.uuid4()}#0"

        oversized = (
            "Jira Service Management request queues accept a customer portal. " * 2000
        ).strip()
        assert len(oversized) > 40960, f"fixture is only {len(oversized)} bytes"

        with pytest.raises((ProviderError, VectorServiceUnavailable)):
            await live_store.upsert([_vector_record(record_id, oversized)], namespace=namespace)

        print(
            f"\n  T027 a {len(oversized):,}-byte chunk is REJECTED "
            f"(limit is 40,960 bytes per vector)"
        )

        await _drop_namespace(live_store, namespace)


# ============================================================================
# The reranker
# ============================================================================


class TestLiveReranking:
    async def test_rerank_reorders_candidates_and_returns_scores(self, live_reranker) -> None:
        """`RankedDocument.index` maps to the position in `documents`.

        The unit tests cover the mapping against a constructed result. This
        covers the part only the service can settle: whether the index it
        returns is relative to the `documents` argument, as documented, or to
        something else.
        """
        from app.retrieval.reranker import RerankCandidate

        candidates = [
            RerankCandidate(
                id=f"u-{i}",
                text=text,
                retrieval_score=0.9 - i * 0.01,
            )
            for i, text in enumerate(
                [
                    "Jira automation rules trigger a transition when a field changes.",
                    "Confluence page properties are set with the content properties API.",
                    "A grocery list, printed weekly, sorted alphabetically.",
                ]
            )
        ]
        relevant_text = candidates[0].text

        ranked = await live_reranker.rerank(
            query="how do I trigger a Jira transition automatically",
            candidates=candidates,
            top_n=3,
        )

        print(f"\n  T027 rerank scores: {[(hit.id, round(hit.rerank_score, 4)) for hit in ranked]}")
        assert len(ranked) == 3
        assert all(0.0 <= hit.rerank_score <= 1.0 for hit in ranked)
        assert [hit.rank for hit in ranked] == [1, 2, 3]

        # The mapping is confirmed when the top hit is the relevant document. A
        # positional mapping bug would put the grocery list first here, so this
        # asserts the mapping end to end rather than just the shape.
        assert ranked[0].text == relevant_text, (
            f"the reranker ranked {ranked[0].text!r} highest for a Jira "
            f"automation question, which suggests `index` does not map to the "
            f"position in `documents`"
        )


# ============================================================================
# Helpers
# ============================================================================


def _vector_record(identifier: str, text: str):
    """A `VectorRecord` with the text in `text` and only real metadata alongside.

    It used to also put `chunk_text` in `metadata`, which the adapter now rejects
    as a collision: the record's text is sent under the schema field name by the
    adapter itself, so repeating it in metadata would be the same value twice
    under one key. The service rejects a nested `metadata` object outright
    (`Invalid type for field 'metadata'`), which is how the flat shape was
    established in the first place.
    """
    from app.retrieval.vector_store import VectorRecord

    return VectorRecord(
        id=identifier,
        text=text,
        metadata={"t027_smoke": True},
    )


#: Shared by the poll helpers. Every record this file writes is about
#: Atlassian configuration, so this finds them all.
_PROBE_QUERY = "atlassian configuration"


async def _poll_for(
    store,
    namespace: str,
    record_id: str,
    *,
    attempts: int = 12,
    query: str = _PROBE_QUERY,
    expect: str | None = None,
):
    """Search until `record_id` appears, or return the last result.

    Integrated indexes are eventually consistent, so a write is not immediately
    visible to a search. A fixed sleep would be either too short on a cold index
    or wastefully long on a warm one; polling for the actual condition is faster
    and more reliable. `attempts` at 1.25s bounds the wait at roughly fifteen
    seconds.

    The default query is deliberately unrelated to `record_id` — an id is not
    text, and searching for one would embed a uuid and match nothing. The records
    written by this file all concern Atlassian configuration, so that query finds
    them.

    `query` and `expect` exist for the truncation test, which needs the opposite:
    a query that matches *only* by a marker in the record's final bytes. Polling
    for `expect` there is what makes "the tail was embedded" a measurement rather
    than an inference from the fact that the record exists.
    """
    import asyncio

    hits: list = []
    for attempt in range(attempts):
        hits = await store.search(query=query, top_k=10, namespace=namespace)
        found = any(hit.id == record_id for hit in hits)
        if expect is not None:
            found = any(hit.id == expect for hit in hits)
        if found:
            return hits
        if attempt < attempts - 1:
            await asyncio.sleep(1.25)

    return hits


async def _poll_until(read, satisfied, attempts: int = 12):
    """Call `read` until `satisfied(result)` or the attempts run out.

    No `store` parameter. It was never used — the closure passed as `read`
    captures whatever it needs — and having it there meant every call site had to
    pass a redundant argument, which is how the one call site came to omit the
    `satisfied` predicate instead and fail on arity.
    """
    import asyncio

    result = await read()
    for attempt in range(attempts):
        if satisfied(result):
            return result
        if attempt < attempts - 1:
            await asyncio.sleep(1.25)
            result = await read()

    return result


async def _search_ids(store, namespace: str) -> set[str]:
    hits = await store.search(query=_PROBE_QUERY, top_k=20, namespace=namespace)
    return {hit.id for hit in hits}


async def _drop_namespace(store, namespace: str) -> None:
    """Remove a smoke-test namespace, ignoring failures.

    Cleanup runs in a `finally` and must never mask the test's own result, so a
    failure here is swallowed. The namespaces are uuid-suffixed and unused by any
    other suite, so an abandoned one is inert rather than harmful.
    """
    try:
        await store.delete_namespace(namespace=namespace)
    except Exception:  # noqa: BLE001, S110 - cleanup only
        pass
