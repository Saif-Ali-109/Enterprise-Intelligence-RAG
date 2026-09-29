"""Live Pinecone verification: the index shape the pipeline actually depends on.

T027. **These tests have never been run.** They require a real `PINECONE_API_KEY`
and a provisioned Pinecone project; no key has been available in this repository's
development environment. They are written so they can be run the moment one is
supplied, and they `skip` with an explicit message rather than passing vacuously
when it is absent.

The distinction this file exists to protect: `tests/unit/test_vendor_adapters.py`
verifies that the adapter reads the SDK's response types correctly, using
responses built from the real SDK classes. It cannot verify that the *service*
behaves as documented. This file is the half that needs the network, and the two
are not substitutes for each other.

## What only a live call can answer

**1. Whether the integrated-embedding index accepts the `embed` dictionary
`ensure_index` sends.** R-002 resolved the *method name* by introspection; it
could not confirm the service accepts `read_parameters`/`write_parameters` in that
form, or what it does with them.

**2. Whether `input_type` is actually persisted and readable.** The unit tests
assert `describe()` reads `input_type` out of a `SemanticTextField`. Nothing has
confirmed the service stores it there, or that `SemanticTextField` appears at all
for an index created this way. If it does not, `input_type_mismatch()` returns
`None` for the wrong reason — the check silently stops checking.

**3. Whether `fields=[]` returns no record data.** The adapter sends an empty
field list deliberately. If the service treats that as "omit the argument" and
returns every field, each query transfers the full chunk text — a silent
egress of the whole corpus on the one request path that runs for every question.
`test_search_returns_no_record_fields` is the assertion that would catch it.

**4. Whether an over-length chunk is truncated or rejected.** This is the one
unresolved question from R-002, and the reason `_TEXT_FIELD` is written with a
`truncate: "END"` directive rather than left to the service default. The answer
determines whether the chunker's 1000-token ceiling is load-bearing or
decorative. **Unknown.** It needs one call with a deliberately long chunk.

**5. Whether the namespace round-trips.** Upsert into a namespace, search it back,
confirm the vector is findable — which exercises the `document_id#ordinal` id
convention end to end rather than in isolation.

## Running these

Requires a Pinecone **serverless** project and a key exported as
`PINECONE_API_KEY`. `ensure_index` creates the index if it is absent, so no
manual setup is needed beyond the project itself. The suite is safe to run
repeatedly; it does not delete the index and only removes the records it wrote.

Do not record a pass for this file until it has been executed against a real
account. A skip is not a pass, and no metric in this repository may be derived
from a suite that has not run.
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio

from tests.fixtures.credentials import has_credential

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not has_credential("PINECONE_API_KEY"),
        reason=(
            "T027 requires a live Pinecone account. No real PINECONE_API_KEY is set — "
            "a placeholder does not count. These tests have never been executed; "
            "a skip is not a pass."
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
    async def test_an_over_length_chunk_is_truncated_rather_than_rejected(self, live_store) -> None:
        """**The behaviour of the hosted embed service is unknown until this runs.**

        The chunker targets 600–1000 tokens, and `ensure_index` requests
        `truncate: "END"`. Whether the service honours that directive, silently
        truncates on its own, or rejects the record outright has never been
        observed, because no live call has been made.

        The two outcomes need different code. If long chunks are rejected, the
        chunker's ceiling is load-bearing and ingestion needs to fail loudly on
        an over-length chunk. If they are silently truncated, records at the
        ceiling lose their tails — and a chunk truncated mid-sentence is a
        chunk whose last citation points at a fragment.

        This test does not assert which happens, because deciding that is the
        point. It performs the call, reports the observed behaviour, and asserts
        only that the record is *findable afterwards* — the property the
        pipeline actually requires. The result is recorded in `research.md` as
        R-002's final open item, and this test's docstring is the record of it.
        """
        namespace = f"t027-smoke-{uuid.uuid4().hex[:8]}"
        record_id = f"{uuid.uuid4()}#0"

        # Roughly 8,000 words — far beyond the 1,000-token ceiling, and beyond
        # the 2,048-token limit of the embedding model, so the service must do
        # something about it.
        oversized = (
            "Jira Service Management request queues accept a customer portal. " * 2000
        ).strip()

        result = await live_store.upsert(
            [_vector_record(record_id, oversized)],
            namespace=namespace,
        )

        print(
            f"\n  T027 over-length chunk ({len(oversized.split())} words): "
            f"acknowledged {result.written}/{result.submitted}"
        )

        if result.written == 0:
            # Rejected. The chunker's ceiling is load-bearing and ingestion must
            # enforce it; `record_chunk` (T041) must raise rather than truncate.
            pytest.fail(
                "the service REJECTED an over-length chunk. The chunker's 1000-token "
                "ceiling is load-bearing; ingestion must reject rather than rely on "
                "server-side truncation."
            )

        # Accepted. Whether it was truncated or not is the service's business;
        # what matters is that the record is retrievable, so a question about
        # its contents finds it rather than silently missing.
        hits = await _poll_for(live_store, namespace, record_id)
        assert hits, (
            "the service ACCEPTED an over-length chunk but it is not findable. "
            "A silently dropped record is worse than a rejected one, because "
            "ingestion would report success."
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
    """A `VectorRecord` carrying the text under the index's mapped field name."""
    from app.retrieval.vector_store import _TEXT_FIELD, VectorRecord

    return VectorRecord(
        id=identifier,
        text=text,
        metadata={_TEXT_FIELD: text, "t027_smoke": True},
    )


async def _poll_for(store, namespace: str, record_id: str, *, attempts: int = 12):
    """Search until `record_id` appears, or return the last result.

    Integrated indexes are eventually consistent, so a write is not immediately
    visible to a search. A fixed sleep would be either too short on a cold index
    or wastefully long on a warm one; polling for the actual condition is faster
    and more reliable. `attempts` at 1.25s bounds the wait at roughly fifteen
    seconds.

    The query is deliberately unrelated to `record_id` — an id is not text, and
    searching for one would embed a uuid and match nothing. The records written
    by this file all concern Atlassian configuration, so that query finds them.
    """
    import asyncio

    for attempt in range(attempts):
        hits = await store.search(query=_PROBE_QUERY, top_k=10, namespace=namespace)
        if any(hit.id == record_id for hit in hits):
            return hits
        if attempt < attempts - 1:
            await asyncio.sleep(1.25)

    return hits


#: Shared by the poll helpers. Every record this file writes is about
#: Atlassian configuration, so this finds them all.
_PROBE_QUERY = "atlassian configuration"


async def _poll_until(store, read, satisfied, attempts: int = 12):
    """Call `read` until `satisfied(result)` or the attempts run out."""
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
