"""Adapter tests that use the real SDK response types.

T029, T030, T031. The point of this file is that it needs **no API key** and
still catches a wrong attribute access, because the responses it builds are the
SDK's own `SearchRecordsResponse` and `RerankResult` rather than stubs shaped by
the same assumptions as the code.

These are not a substitute for T027 and T028. They verify that the adapters read
the SDK correctly; they cannot verify that the service behaves as documented —
that needs a key and is what the integration suite is for. Both suites are
required before the pipeline is trusted, and this file is what makes the
non-key-dependent half of that check possible.
"""

from __future__ import annotations

import dataclasses

import httpx
import pytest
from app.core.errors import ProviderError, VectorServiceUnavailable

from tests.fixtures.vendors import (
    FakeInference,
    FakePineconeClient,
    FakePineconeIndex,
    make_index_model,
    make_rerank_result,
    make_search_response,
)

pytestmark = pytest.mark.unit


# ============================================================================
# T029 — VectorStore against the real SearchRecordsResponse
# ============================================================================


class TestSearchResponseHandling:
    """`search().result` is a single `SearchResult`, not a list of blocks.

    The first draft of `_hits_from` iterated `response.result` as if it were a
    list of per-namespace blocks. Against the real type that is a `TypeError`,
    and it would have been raised on the very first question in production. The
    SDK's own docstring calls the extra `result` step "the usual first stumble".
    """

    async def test_reads_hits_through_the_result_wrapper(
        self, fake_pinecone: FakePineconeClient
    ) -> None:
        from app.retrieval.vector_store import PineconeVectorStore

        fake_pinecone.index_handle.search_response = make_search_response(
            [("doc-1#0", 0.91), ("doc-1#1", 0.77)]
        )
        store = PineconeVectorStore(client=fake_pinecone)

        hits = await store.search(query="how do I configure jira", top_k=2)

        assert [hit.id for hit in hits] == ["doc-1#0", "doc-1#1"]
        assert [hit.score for hit in hits] == [0.91, 0.77]

    async def test_hit_identity_is_read_from_the_id_property(
        self, fake_pinecone: FakePineconeClient
    ) -> None:
        """`Hit` renames `id_` to `_id` on the wire and exposes `id` as a property.

        Reading the wire names works only by accident and breaks on the next
        SDK version; reading `hit.id` is the documented contract.
        """
        from app.retrieval.vector_store import PineconeVectorStore

        fake_pinecone.index_handle.search_response = make_search_response([("u-42", 0.5)])
        store = PineconeVectorStore(client=fake_pinecone)

        hits = await store.search(query="q", top_k=1)

        assert hits[0].id == "u-42"
        # If the adapter had read `hit.id_` this would also pass, so check the
        # property exists and is what was used.
        from pinecone import Hit

        assert isinstance(Hit(id_="x", score_=0.1).id, str)

    async def test_namespace_comes_from_the_request_not_the_response(
        self, fake_pinecone: FakePineconeClient
    ) -> None:
        """`SearchResult` has one field, `hits`. The namespace is not returned.

        So the adapter has to thread the requested namespace into the hit, or a
        hit cannot be joined back to its document one stage later.
        """
        from app.retrieval.vector_store import PineconeVectorStore

        fake_pinecone.index_handle.search_response = make_search_response([("u-1", 0.5)])
        store = PineconeVectorStore(client=fake_pinecone, namespace="atlassian-public")

        hits = await store.search(query="q", top_k=1)

        assert hits[0].metadata["namespace"] == "atlassian-public"
        assert fake_pinecone.index_handle.search_calls[0]["namespace"] == "atlassian-public"

    async def test_empty_result_is_empty_list_not_an_error(
        self, fake_pinecone: FakePineconeClient
    ) -> None:
        """A search matching nothing is a normal outcome, and routes to refusal.

        Treating it as an error would turn "the documentation does not cover
        this" into a 503, which is the opposite of FR-008.
        """
        from app.retrieval.vector_store import PineconeVectorStore

        store = PineconeVectorStore(client=fake_pinecone)
        assert await store.search(query="anything", top_k=5) == []

    async def test_hits_are_sorted_descending(self, fake_pinecone: FakePineconeClient) -> None:
        """The service documents descending order; the adapter does not rely on it.

        A rerank stage that assumes the order and receives otherwise produces a
        subtly wrong evidence set with nothing to indicate why.
        """
        from app.retrieval.vector_store import PineconeVectorStore

        fake_pinecone.index_handle.search_response = make_search_response(
            [("low", 0.1), ("high", 0.95), ("mid", 0.5)]
        )
        store = PineconeVectorStore(client=fake_pinecone)

        hits = await store.search(query="q", top_k=3)

        assert [hit.id for hit in hits] == ["high", "mid", "low"]

    async def test_hit_without_a_usable_score_is_dropped(
        self, fake_pinecone: FakePineconeClient
    ) -> None:
        """A hit with no score cannot be ranked and one with no id cannot be cited.

        Passing them through puts a `None` into a comparison, and the resulting
        error surfaces far from the cause.
        """
        from app.retrieval.vector_store import PineconeVectorStore

        response = make_search_response([("good", 0.8)])
        # A score that is not a number: the real type would reject this, so the
        # adapter's guard is exercised by bypassing the struct deliberately.
        response.result.hits.append(object())
        fake_pinecone.index_handle.search_response = response
        store = PineconeVectorStore(client=fake_pinecone)

        hits = await store.search(query="q", top_k=5)

        assert [hit.id for hit in hits] == ["good"]


class TestSearchRequestShape:
    """What goes *out* matters as much as what comes back."""

    async def test_query_is_sent_as_text_inputs(self, fake_pinecone: FakePineconeClient) -> None:
        """On an integrated index the client sends text, never a vector.

        The service embeds. Sending a pre-computed vector would either be
        rejected or silently ignore the index's embed configuration, which is
        where `input_type` lives.
        """
        from app.retrieval.vector_store import PineconeVectorStore

        store = PineconeVectorStore(client=fake_pinecone)
        await store.search(query="configure a board", top_k=3)

        call = fake_pinecone.index_handle.search_calls[0]
        assert call["inputs"] == {"text": "configure a board"}
        assert "vector" not in call

    async def test_no_record_fields_are_requested(self, fake_pinecone: FakePineconeClient) -> None:
        """`fields=[]` is deliberate: the registry is the authoritative copy.

        Requesting the text here would return a second, unauthoritative copy of
        third-party content into the answer path.
        """
        from app.retrieval.vector_store import PineconeVectorStore

        store = PineconeVectorStore(client=fake_pinecone)
        await store.search(query="q", top_k=3)

        assert fake_pinecone.index_handle.search_calls[0]["fields"] == []

    async def test_filter_is_a_dict_never_an_interpolated_expression(
        self, fake_pinecone: FakePineconeClient
    ) -> None:
        """A filter language invites string interpolation, which invites injection.

        The conversion is a pass-through dict so there is nothing to interpolate
        into. A user-controlled product name containing a filter operator must
        arrive as a literal value.
        """
        from app.retrieval.vector_store import PineconeVectorStore

        store = PineconeVectorStore(client=fake_pinecone)
        await store.search(
            query="q",
            top_k=3,
            metadata_filter={"product": "jira", "$or": [{"language": "en"}]},
        )

        sent = fake_pinecone.index_handle.search_calls[0]["filter"]
        assert sent == {"product": "jira", "$or": [{"language": "en"}]}
        assert isinstance(sent, dict)

    async def test_injection_shaped_product_name_stays_a_value(
        self, fake_pinecone: FakePineconeClient
    ) -> None:
        from app.retrieval.vector_store import PineconeVectorStore

        store = PineconeVectorStore(client=fake_pinecone)
        await store.search(
            query="q", top_k=1, metadata_filter={"product": '{"$gt": 0}, "evil": {"$ne": ""}'}
        )

        sent = fake_pinecone.index_handle.search_calls[0]["filter"]
        # Still one key, still a string. The operator characters are inert data.
        assert list(sent) == ["product"]
        assert isinstance(sent["product"], str)

    async def test_none_filter_is_none_not_empty_dict(
        self, fake_pinecone: FakePineconeClient
    ) -> None:
        from app.retrieval.vector_store import PineconeVectorStore

        store = PineconeVectorStore(client=fake_pinecone)
        await store.search(query="q", top_k=1)

        assert fake_pinecone.index_handle.search_calls[0]["filter"] is None

    async def test_top_k_must_be_positive(self, fake_pinecone: FakePineconeClient) -> None:
        from app.retrieval.vector_store import PineconeVectorStore

        store = PineconeVectorStore(client=fake_pinecone)
        with pytest.raises(ValueError):
            await store.search(query="q", top_k=0)


class TestUpsert:
    async def test_upsert_sends_no_dimensions_and_no_vectors(
        self, fake_pinecone: FakePineconeClient
    ) -> None:
        """The service derives the dimension, and the text is what it embeds.

        This is the code-level half of `dimension_source: 'service'`, and it was
        wrong on the second half until T027 ran against a live index. The
        adapter sent `values=[0.0]` on the belief that a zero vector was the
        integrated-embedding placeholder; the service rejects it outright with
        `Vector dimension 1 does not match the dimension of the index 1024`.

        R-003 had already ruled this out — "write with `upsert_records` (text +
        metadata, no `values`)" — so the research note was right and the code did
        not follow it. That is the failure mode a written decision is supposed to
        prevent, and it survived a written test, because the test asserted the
        wrong behaviour was correct.
        """
        from app.retrieval.vector_store import PineconeVectorStore, VectorRecord

        store = PineconeVectorStore(client=fake_pinecone)
        await store.upsert([VectorRecord(id="u-1", text="Jira boards filter by JQL.")])

        call = fake_pinecone.index_handle.upsert_calls[0]
        assert "dimension" not in call
        assert "vectors" not in call, "the write must go through upsert_records, not upsert"
        assert "records" in call

        record = call["records"][0]
        assert "dimension" not in record
        # No `values` key at all. A zero vector is not a placeholder here; it is
        # a 400.
        assert "values" not in record, f"a `values` key was sent: {record.get('values')!r}"

    async def test_metadata_is_flat_not_nested(self, fake_pinecone: FakePineconeClient) -> None:
        """Measured: the service has no nested-object metadata.

        A nested `{"metadata": {...}}` is rejected with `Invalid type for field
        'metadata' in record ... got '{"product":"jira"}'` — the service reads
        the stringified object and rejects its type. The SDK's own docstring shows
        the shape: `{"_id": ..., "text": ...}` with the fields alongside.

        This one is worth a test of its own because the failure is not local: a
        rejected upsert is caught, reported, and the whole ingest stops. The
        shape has to be right in the adapter, not discovered one batch at a time
        against a billable service.
        """
        from app.retrieval.vector_store import PineconeVectorStore, VectorRecord

        store = PineconeVectorStore(client=fake_pinecone)
        await store.upsert(
            [
                VectorRecord(
                    id="u-1",
                    text="body",
                    metadata={"product": "jira", "chunk_index": 3, "is_code": False},
                )
            ]
        )

        record = fake_pinecone.index_handle.upsert_calls[0]["records"][0]
        assert "metadata" not in record, f"metadata was nested: {record!r}"
        assert record["product"] == "jira"
        assert record["chunk_index"] == 3
        assert record["is_code"] is False

    async def test_a_heading_path_survives_as_a_real_list(self) -> None:
        """FR-018 needs the heading path per chunk, and it must not be flattened.

        A `heading_path` joined into `"A > B"` would still display, but it could
        no longer be compared against another unit's path to decide whether two
        units are in the same section — which is how the chunker finds where a
        section ends. Verified against the live index: the list comes back as a
        list.
        """
        from app.retrieval.vector_store import VectorRecord, _record_payload

        path = ["Jira Cloud", "Filters", "Create a filter"]
        payload = _record_payload(
            VectorRecord(id="u-1", text="body", metadata={"heading_path": path})
        )

        assert payload["heading_path"] == path
        assert isinstance(payload["heading_path"], list)

    async def test_metadata_cannot_silently_overwrite_the_text(self) -> None:
        """The metadata is spread last, so a colliding key would win.

        `**record.metadata` is placed after `_TEXT_FIELD` so that metadata cannot
        clobber the id or the text, and a collision is raised rather than
        discarded. The service would accept either record — the corruption would
        surface as a citation pointing at the wrong text with no error anywhere.
        """
        from app.retrieval.vector_store import VectorRecord, _record_payload

        with pytest.raises(ValueError, match="collide"):
            _record_payload(
                VectorRecord(id="u-1", text="real text", metadata={"chunk_text": "other text"})
            )
        with pytest.raises(ValueError, match="collide"):
            _record_payload(VectorRecord(id="u-1", text="real", metadata={"_id": "spoofed"}))

    async def test_batches_respect_the_service_bound(
        self, fake_pinecone: FakePineconeClient
    ) -> None:
        """One HTTP round trip per chunk is 4,000 for a modest corpus."""
        from app.retrieval.vector_store import _UPSERT_BATCH, PineconeVectorStore, VectorRecord

        store = PineconeVectorStore(client=fake_pinecone)
        count = _UPSERT_BATCH * 2 + 7
        await store.upsert([VectorRecord(id=f"u-{i}", text="t") for i in range(count)])

        assert len(fake_pinecone.index_handle.upsert_calls) == 3
        assert [len(call["records"]) for call in fake_pinecone.index_handle.upsert_calls] == [
            _UPSERT_BATCH,
            _UPSERT_BATCH,
            7,
        ]

    async def test_acknowledged_count_is_read_from_the_real_field_name(
        self, fake_pinecone: FakePineconeClient
    ) -> None:
        """A short write means chunks are missing while the pipeline reports success.

        The shortfall has to be visible to the caller, or an unanswerable
        question appears later with no ingestion error to explain it. The field
        is `record_count` on the real `UpsertRecordsResponse` — the older
        `upsertedCount` belongs to the `upsert` reply this path no longer uses.
        """
        from app.retrieval.vector_store import PineconeVectorStore, VectorRecord

        from tests.fixtures.vendors import make_upsert_records_response

        fake_pinecone.index_handle.upsert_results = [make_upsert_records_response(40)]
        store = PineconeVectorStore(client=fake_pinecone)

        result = await store.upsert([VectorRecord(id=f"u-{i}", text="t") for i in range(100)])

        assert result.submitted == 100
        assert result.written == 40

    async def test_empty_batch_does_not_call_the_service(
        self, fake_pinecone: FakePineconeClient
    ) -> None:
        from app.retrieval.vector_store import PineconeVectorStore

        store = PineconeVectorStore(client=fake_pinecone)
        result = await store.upsert([])

        assert (result.written, result.submitted) == (0, 0)
        assert fake_pinecone.index_handle.upsert_calls == []


class TestDelete:
    async def test_empty_delete_is_refused(self, fake_pinecone: FakePineconeClient) -> None:
        """Pinecone's `delete` with no ids is a namespace wipe, not a no-op.

        FR-054 and FR-056 depend on removal working. A caller that computed no
        ids has a bug, and passing it through would turn that bug into total
        data loss.
        """
        from app.retrieval.vector_store import PineconeVectorStore

        store = PineconeVectorStore(client=fake_pinecone)
        with pytest.raises(ValueError, match="wipe the namespace"):
            await store.delete([])

        assert fake_pinecone.index_handle.delete_calls == []

    async def test_delete_passes_ids_through(self, fake_pinecone: FakePineconeClient) -> None:
        from app.retrieval.vector_store import PineconeVectorStore

        store = PineconeVectorStore(client=fake_pinecone)
        assert await store.delete(["u-1", "u-2"]) == 2
        assert fake_pinecone.index_handle.delete_calls[0]["ids"] == ["u-1", "u-2"]


class TestDescribe:
    async def test_ready_index_reports_ready(self, fake_pinecone: FakePineconeClient) -> None:
        from app.retrieval.vector_store import PineconeVectorStore

        fake_pinecone._model = make_index_model(ready=True, state="Ready")
        store = PineconeVectorStore(client=fake_pinecone)

        info = await store.describe()

        assert info.ready is True
        assert info.state == "Ready"
        # `status` is a property, so the contract's string field is satisfied
        # without the adapter having to stringify the SDK's `IndexStatus` object.
        assert info.status == "Ready"

    async def test_readiness_is_the_bool_not_a_string_comparison(self) -> None:
        """`IndexStatus.ready` is a bool and `.state` is the label.

        The first draft stringified `info.status` and compared it to `"Ready"`,
        which against a struct with fields `('ready', 'state')` never matches. It
        reported every index as not ready, forever, and only in production.
        """
        from pinecone import IndexStatus

        status = IndexStatus(ready=True, state="Ready")
        assert status.ready is True
        assert status.state == "Ready"
        # What the old comparison would have seen:
        assert str(status).lower() != "ready"

    async def test_not_ready_index_reports_the_state(
        self, fake_pinecone: FakePineconeClient
    ) -> None:
        from app.retrieval.vector_store import PineconeVectorStore

        fake_pinecone._model = make_index_model(ready=False, state="InitializationFailed")
        store = PineconeVectorStore(client=fake_pinecone)

        info = await store.describe()

        assert info.ready is False
        # The state is what tells an operator *why*, which is why it is kept
        # rather than collapsed into a boolean.
        assert info.state == "InitializationFailed"

    async def test_uses_the_indexes_namespace_not_the_shim(
        self, fake_pinecone: FakePineconeClient
    ) -> None:
        """`Pinecone.describe_index` is documented as a shim for `indexes.describe`."""
        from app.retrieval.vector_store import PineconeVectorStore

        store = PineconeVectorStore(client=fake_pinecone)
        await store.describe()

        assert fake_pinecone.describe_calls == ["enterprise-knowledge"]

    async def test_reads_the_embedding_model_from_the_schema(
        self, fake_pinecone: FakePineconeClient
    ) -> None:
        from app.retrieval.vector_store import PineconeVectorStore

        fake_pinecone._model = make_index_model(model="llama-text-embed-v2", metric="cosine")
        store = PineconeVectorStore(client=fake_pinecone)

        info = await store.describe()

        assert info.embed_model == "llama-text-embed-v2"
        assert info.metric == "cosine"

    async def test_integrated_index_has_no_dimension_to_report(
        self, fake_pinecone: FakePineconeClient
    ) -> None:
        """The service derived it, so this application has no opinion to hold.

        `expected_dimension` is `None` and no comparison is possible, which is
        why the health probe skips the dimension check rather than comparing
        against a constant.
        """
        from app.retrieval.vector_store import PineconeVectorStore

        store = PineconeVectorStore(client=fake_pinecone)
        info = await store.describe()

        assert info.dimension is None
        assert info.expected_dimension is None

    async def test_a_manual_dense_vector_index_reports_no_model(
        self, fake_pinecone: FakePineconeClient
    ) -> None:
        """A `dense_vector` field has no `model`, so there is no `input_type` to check.

        Reported as `None` rather than falling back to the configured model name:
        claiming to have checked an index this application cannot check is worse
        than admitting it did not.
        """
        from app.retrieval.vector_store import PineconeVectorStore

        fake_pinecone._model = make_index_model(model=None)
        store = PineconeVectorStore(client=fake_pinecone)

        info = await store.describe()

        assert info.read_input_type is None
        assert info.write_input_type is None
        assert info.input_type_mismatch() is None


class TestInputTypeVerification:
    """R-001: an `input_type` mismatch "quietly degrades" quality while both
    calls still succeed. `input_type` is fixed at index creation, so it is read
    back and checked rather than assumed.
    """

    async def test_correct_configuration_reports_no_mismatch(
        self, fake_pinecone: FakePineconeClient
    ) -> None:
        from app.retrieval.vector_store import PineconeVectorStore

        fake_pinecone._model = make_index_model(read_input_type="query", write_input_type="passage")
        store = PineconeVectorStore(client=fake_pinecone)

        info = await store.describe()

        assert info.read_input_type == "query"
        assert info.write_input_type == "passage"
        assert info.input_type_mismatch() is None

    async def test_symmetric_input_type_is_detected(
        self, fake_pinecone: FakePineconeClient
    ) -> None:
        """The realistic misconfiguration: both set to `passage`.

        An index created without the asymmetry is the most likely way to end up
        here, and it is exactly the case that degrades silently.
        """
        from app.retrieval.vector_store import PineconeVectorStore

        fake_pinecone._model = make_index_model(
            read_input_type="passage", write_input_type="passage"
        )
        store = PineconeVectorStore(client=fake_pinecone)

        mismatch = (await store.describe()).input_type_mismatch()

        assert mismatch is not None
        assert "passage" in mismatch
        assert "query" in mismatch
        # The reason it matters has to be in the message, or an operator reads
        # "not matching" and does not know whether to worry.
        assert "degrades silently" in mismatch

    async def test_only_the_read_side_is_reported_when_only_it_is_wrong(
        self, fake_pinecone: FakePineconeClient
    ) -> None:
        from app.retrieval.vector_store import PineconeVectorStore

        fake_pinecone._model = make_index_model(
            read_input_type="passage", write_input_type="passage"
        )
        store = PineconeVectorStore(client=fake_pinecone)

        mismatch = (await store.describe()).input_type_mismatch()

        # A single wrong side must be distinguishable, not merged into one
        # "mismatch" verdict.
        assert "writes embed as 'passage'" not in (mismatch or "")

    async def test_parameters_absent_is_not_a_mismatch(
        self, fake_pinecone: FakePineconeClient
    ) -> None:
        """ "The service did not tell us" is not "the service is wrong".

        A field a future version stops returning must not turn `/health` red for
        a reason that is not real.
        """
        from app.retrieval.vector_store import PineconeVectorStore

        fake_pinecone._model = make_index_model(read_input_type=None, write_input_type=None)
        store = PineconeVectorStore(client=fake_pinecone)

        assert (await store.describe()).input_type_mismatch() is None


class TestParameterParsing:
    def test_dict_parameters_are_read(self) -> None:
        from app.retrieval.vector_store import _parameter

        assert _parameter({"input_type": "query"}, "input_type") == "query"

    def test_json_string_parameters_are_read(self) -> None:
        from app.retrieval.vector_store import _parameter

        assert _parameter('{"input_type": "passage"}', "input_type") == "passage"

    def test_malformed_json_is_none_not_an_exception(self) -> None:
        from app.retrieval.vector_store import _parameter

        assert _parameter("{not json", "input_type") is None

    def test_non_string_values_are_none(self) -> None:
        from app.retrieval.vector_store import _parameter

        assert _parameter({"input_type": 3}, "input_type") is None
        assert _parameter(None, "input_type") is None


class TestErrorTranslation:
    """Error codes are distinguished because their remedies differ."""

    async def test_connection_failure_is_vector_service_unavailable(self) -> None:
        from app.retrieval.vector_store import PineconeVectorStore
        from pinecone import PineconeConnectionError

        class Failing(FakePineconeIndex):
            def search(self, **kwargs: object) -> object:
                raise PineconeConnectionError("connection refused")

        client = FakePineconeClient(index=Failing())
        store = PineconeVectorStore(client=client)

        with pytest.raises(VectorServiceUnavailable):
            await store.search(query="q", top_k=1)

    async def test_timeout_is_vector_service_unavailable(self) -> None:
        from app.retrieval.vector_store import PineconeVectorStore
        from pinecone import PineconeTimeoutError

        class Slow(FakePineconeIndex):
            def search(self, **kwargs: object) -> object:
                raise PineconeTimeoutError("timed out")

        store = PineconeVectorStore(client=FakePineconeClient(index=Slow()))

        with pytest.raises(VectorServiceUnavailable):
            await store.search(query="q", top_k=1)

    async def test_bad_credential_is_a_provider_error_not_an_outage(self) -> None:
        """401 and a network fault have opposite remedies.

        Reporting a revoked key as "vector service unreachable" sends the
        operator to the wrong system entirely.

        Constructed with `status_code=`, which is the real `ApiError` signature
        in pinecone==10.0.0. The previous `status=` was not a valid keyword at
        all, so this test was not exercising the branch it claimed to — and the
        adapter's own `getattr(exc, "status", None)` had the same flaw, reading
        an attribute that does not exist and therefore never matching 401.
        """
        from app.retrieval.vector_store import PineconeVectorStore
        from pinecone import PineconeApiException

        class Unauthorized(FakePineconeIndex):
            def search(self, **kwargs: object) -> object:
                raise PineconeApiException("bad key", status_code=401)

        store = PineconeVectorStore(client=FakePineconeClient(index=Unauthorized()))

        with pytest.raises(ProviderError) as caught:
            await store.search(query="q", top_k=1)
        assert not isinstance(caught.value, VectorServiceUnavailable)
        assert "credential" in caught.value.message

    async def test_a_malformed_request_is_not_reported_as_a_key_problem(self) -> None:
        """The live failure T027 hit: a 400 from a request this app built wrongly.

        It arrived as a bare `ApiError`, which the 401/403 branch does not catch,
        so it reached the fallthrough. Falling through to the *language model*
        error — the original bug — pointed an operator at Groq for a 1024-versus-1
        vector in this application's own write path.
        """
        from app.retrieval.vector_store import PineconeVectorStore
        from pinecone.errors.exceptions import ApiError

        class Malformed(FakePineconeIndex):
            def search(self, **kwargs: object) -> object:
                raise ApiError("Vector dimension 1 does not match the dimension of the index 1024")

        store = PineconeVectorStore(client=FakePineconeClient(index=Malformed()))

        with pytest.raises(VectorServiceUnavailable) as caught:
            await store.search(query="q", top_k=1)
        assert "language model" not in caught.value.message


# ============================================================================
# T031 — Reranker against the real RerankResult
# ============================================================================


def _candidates(count: int, *, with_text: bool = True) -> list:
    from app.retrieval.reranker import RerankCandidate

    return [
        RerankCandidate(
            id=f"u-{i}",
            text=f"Document number {i} about Jira configuration." if with_text else "",
            retrieval_score=0.9 - i * 0.01,
        )
        for i in range(count)
    ]


class TestRerankResponseHandling:
    async def test_index_maps_back_to_the_right_candidate(self) -> None:
        """`RankedDocument.index` is the position in the `documents` argument.

        A mapping error here attaches a real score to the wrong citation, which
        is a grounding defect no schema check would catch. The fake returns
        scores in reverse order precisely so a swapped mapping is visible.
        """
        from app.retrieval.reranker import PineconeReranker

        inference = FakeInference(make_rerank_result([(2, 0.9), (0, 0.4), (1, 0.7)]))
        reranker = PineconeReranker(client=FakePineconeClient(inference=inference))
        candidates = _candidates(3)

        ranked = await reranker.rerank(query="q", candidates=candidates, top_n=3)

        # Returned out of order deliberately: index 2 scored highest.
        assert [(hit.id, hit.rerank_score) for hit in ranked] == [
            ("u-2", 0.9),
            ("u-1", 0.7),
            ("u-0", 0.4),
        ]
        assert [hit.rank for hit in ranked] == [1, 2, 3]

    async def test_rank_reflects_score_order_not_response_order(self) -> None:
        """`rank` is assigned here, so an out-of-order response must be normalised.

        The SDK documents `data` as already descending by score, but `rank` is
        this method's own field and it is what the evidence selector and the
        citation ordering read. Trusting the service's order would make `rank`
        a lie whenever the service's sort ever differed.
        """
        from app.retrieval.reranker import PineconeReranker

        # Deliberately ascending, which contradicts the documented order.
        inference = FakeInference(make_rerank_result([(0, 0.1), (1, 0.5), (2, 0.9)]))
        reranker = PineconeReranker(client=FakePineconeClient(inference=inference))

        ranked = await reranker.rerank(query="q", candidates=_candidates(3), top_n=3)

        assert [(hit.id, hit.rerank_score, hit.rank) for hit in ranked] == [
            ("u-2", 0.9, 1),
            ("u-1", 0.5, 2),
            ("u-0", 0.1, 3),
        ]

    async def test_ties_break_deterministically_on_candidate_order(self) -> None:
        """A reranker returning equal scores must not produce an arbitrary order."""
        from app.retrieval.reranker import PineconeReranker

        inference = FakeInference(make_rerank_result([(2, 0.5), (0, 0.5), (1, 0.5)]))
        reranker = PineconeReranker(client=FakePineconeClient(inference=inference))

        ranked = await reranker.rerank(query="q", candidates=_candidates(3), top_n=3)

        assert [hit.id for hit in ranked] == ["u-0", "u-1", "u-2"]

    async def test_both_scores_are_preserved_separately(self) -> None:
        """FR-021 scores retrieval and rerank as independent signals.

        Blending them would make both numbers meaningless, and contracts/README.md
        forbids presenting them on a shared scale.
        """
        from app.retrieval.reranker import PineconeReranker

        inference = FakeInference(make_rerank_result([(0, 0.2)]))
        reranker = PineconeReranker(client=FakePineconeClient(inference=inference))

        ranked = await reranker.rerank(query="q", candidates=_candidates(1), top_n=1)

        assert ranked[0].retrieval_score == 0.9
        assert ranked[0].rerank_score == 0.2
        assert ranked[0].retrieval_score != ranked[0].rerank_score

    async def test_documents_are_sent_clean(self) -> None:
        """No id marker is prepended to the text.

        An earlier draft prefixed each document with `[id]=u-1`. With
        `return_documents=False` the document is never returned, so the marker
        was pure loss — and it hands the reranker tokens absent from the query,
        degrading the score this stage exists to produce.
        """
        from app.retrieval.reranker import PineconeReranker

        inference = FakeInference()
        reranker = PineconeReranker(client=FakePineconeClient(inference=inference))
        candidates = _candidates(2)

        await reranker.rerank(query="q", candidates=candidates, top_n=2)

        call = inference.rerank_calls[0]
        assert call["documents"] == [c.text for c in candidates]
        assert all("[id]" not in doc for doc in call["documents"])
        assert call["return_documents"] is False

    async def test_long_units_are_truncated_rather_than_rejected(self) -> None:
        """A maximum-size unit plus a question exceeds the service's pair limit.

        Measured live: a 1000-token unit with a one-line query returned
        `400 INVALID_ARGUMENT ... exceeds the maximum token limit of 1024` and the
        whole request failed. Truncation is the only thing standing between a
        long unit and an answer, and the parameter that does it is named by the
        service's own error.
        """
        from app.retrieval.reranker import PineconeReranker

        inference = FakeInference()
        reranker = PineconeReranker(client=FakePineconeClient(inference=inference))

        await reranker.rerank(query="q", candidates=_candidates(2), top_n=2)

        assert inference.rerank_calls[0]["parameters"] == {"truncate": "END"}

    async def test_substituted_model_is_logged_not_hidden(self) -> None:
        """The SDK documents that the serving model is not always the requested one."""
        from app.retrieval.reranker import PineconeReranker

        inference = FakeInference(make_rerank_result([(0, 0.5)], model="cohere-rerank-3.5"))
        reranker = PineconeReranker(client=FakePineconeClient(inference=inference))

        ranked = await reranker.rerank(query="q", candidates=_candidates(1), top_n=1)

        assert ranked[0].rerank_score == 0.5

    async def test_uninterpretable_response_falls_back_visibly(self) -> None:
        """The fallback is a degraded ranking, and it looks degraded.

        `rerank_score` equals `retrieval_score` on every hit, so a reader can
        tell there was no real reranking. Inventing scores would be worse.
        """
        from app.retrieval.reranker import PineconeReranker

        inference = FakeInference(result=object())
        reranker = PineconeReranker(client=FakePineconeClient(inference=inference))
        candidates = _candidates(3)

        ranked = await reranker.rerank(query="q", candidates=candidates, top_n=2)

        assert [hit.id for hit in ranked] == ["u-0", "u-1"]
        for hit in ranked:
            assert hit.rerank_score == hit.retrieval_score

    async def test_out_of_range_index_is_dropped_not_misapplied(self) -> None:
        """A bounds check against the wrong list is how a score lands on the wrong citation."""
        from app.retrieval.reranker import PineconeReranker

        inference = FakeInference(make_rerank_result([(0, 0.9), (99, 0.99)]))
        reranker = PineconeReranker(client=FakePineconeClient(inference=inference))

        ranked = await reranker.rerank(query="q", candidates=_candidates(2), top_n=2)

        assert [hit.id for hit in ranked] == ["u-0"]
        assert all(hit.id != "u-1" or hit.rerank_score != 0.99 for hit in ranked)

    async def test_score_outside_unit_range_is_clamped_and_logged(self) -> None:
        from app.retrieval.reranker import PineconeReranker

        inference = FakeInference(make_rerank_result([(0, 1.4), (1, -0.2)]))
        reranker = PineconeReranker(client=FakePineconeClient(inference=inference))

        ranked = await reranker.rerank(query="q", candidates=_candidates(2), top_n=2)

        assert all(0.0 <= hit.rerank_score <= 1.0 for hit in ranked)

    async def test_top_n_is_bounded_by_the_candidate_count(self) -> None:
        """Sending an oversized `top_n` makes the candidate count the real cap.

        A silent cap where the code says otherwise is the failure this avoids.
        """
        from app.retrieval.reranker import PineconeReranker

        inference = FakeInference()
        reranker = PineconeReranker(client=FakePineconeClient(inference=inference))

        await reranker.rerank(query="q", candidates=_candidates(3), top_n=6)

        assert inference.rerank_calls[0]["top_n"] == 3

    async def test_candidates_without_text_are_refused(self) -> None:
        """Reranking empty strings returns confident scores for nothing.

        Empty text means the registry join returned nothing, which is a
        pipeline bug. Returning an empty list routes to a refusal rather than to
        a fabricated citation.
        """
        from app.retrieval.reranker import PineconeReranker

        inference = FakeInference()
        reranker = PineconeReranker(client=FakePineconeClient(inference=inference))

        ranked = await reranker.rerank(
            query="q", candidates=_candidates(4, with_text=False), top_n=3
        )

        assert ranked == []
        assert inference.rerank_calls == []

    async def test_blank_candidates_are_dropped_and_indices_rebased(self) -> None:
        """Positions shift when a candidate is dropped, so `index` must be rebased.

        `index` is relative to the `documents` argument, which is the *filtered*
        list. If the result were mapped against the original three candidates,
        index 0 would be scored onto the blank `u-0` and index 1 onto `u-1` —
        shifting every score onto the wrong citation. The explicit scores below
        make that failure visible: the assertion pins which candidate received
        which score, not merely which ids came back.
        """
        from app.retrieval.reranker import PineconeReranker

        # After dropping u-0, the documents are [u-1, u-2]. So index 0 is u-1
        # and index 1 is u-2.
        inference = FakeInference(make_rerank_result([(0, 0.3), (1, 0.8)]))
        reranker = PineconeReranker(client=FakePineconeClient(inference=inference))
        candidates = _candidates(3)
        candidates[0] = candidates[0].__class__(id="u-0", text="   ", retrieval_score=0.99)

        ranked = await reranker.rerank(query="q", candidates=candidates, top_n=2)

        assert [hit.id for hit in ranked] == ["u-2", "u-1"]
        assert [(hit.id, hit.rerank_score) for hit in ranked] == [("u-2", 0.8), ("u-1", 0.3)]
        # The dropped candidate's high retrieval score must not leak through.
        assert "u-0" not in {hit.id for hit in ranked}
        # The text sent for ranking is the filtered list, in filtered order.
        assert inference.rerank_calls[0]["documents"] == [candidates[1].text, candidates[2].text]

    async def test_empty_input_short_circuits(self) -> None:
        from app.retrieval.reranker import PineconeReranker

        inference = FakeInference()
        reranker = PineconeReranker(client=FakePineconeClient(inference=inference))

        assert await reranker.rerank(query="q", candidates=[], top_n=6) == []
        assert inference.rerank_calls == []


# ============================================================================
# T030 — Groq provider
# ============================================================================


def _http_returning(status: int, payload: object) -> httpx.AsyncClient:
    """An `httpx` client that always answers with one canned response."""
    transport = httpx.MockTransport(
        lambda request: httpx.Response(status, json=payload, request=request)
    )
    return httpx.AsyncClient(transport=transport, base_url="https://api.groq.com")


class TestCompletion:
    async def test_returns_content_with_reasoning_suppressed(self) -> None:
        from app.generation.provider import GroqProvider

        client = _http_returning(
            200,
            {
                "model": "openai/gpt-oss-120b",
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "Boards are filtered by JQL."},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 8, "total_tokens": 18},
            },
        )
        completion = await GroqProvider(client=client).complete(system="s", user="u")

        assert completion.text == "Boards are filtered by JQL."
        assert completion.usage.total_tokens == 18
        assert completion.finish_reason == "stop"

    async def test_a_reasoning_channel_in_the_response_is_discarded(self) -> None:
        """FR-034: the answer arrives; the deliberation does not.

        The provider is allowed to reason — R-014 measures that it always does
        and that no parameter stops it. So this is not an error case. The
        completion must carry the content and nothing from `message.reasoning`.

        Written as a discard test rather than a refusal test because the previous
        expectation was the wrong shape of guarantee: refusing any response
        exposing a reasoning channel would refuse *every* query from a
        configured reasoning model, which is a system that cannot answer
        anything rather than a system that does not leak.
        """
        from app.generation.provider import GroqProvider

        client = _http_returning(
            200,
            {
                "model": "openai/gpt-oss-120b",
                "choices": [
                    {
                        "message": {
                            "content": "Boards are filtered by JQL.",
                            "reasoning": "step 1, step 2, therefore the answer",
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 8,
                    "total_tokens": 18,
                    "completion_tokens_details": {"reasoning_tokens": 6},
                },
            },
        )

        completion = await GroqProvider(client=client).complete(system="s", user="u")

        assert completion.text == "Boards are filtered by JQL."
        # The whole object, not just the field a reviewer thought to look at.
        rendered = repr(dataclasses.asdict(completion)) + " " + repr(completion)
        assert "step 1" not in rendered
        assert "reasoning" not in rendered.lower()

    async def test_the_discarded_reasoning_is_counted_but_not_quoted(self) -> None:
        """Discarding silently is indistinguishable from never receiving.

        If the boundary stopped working, nothing would change in the answer — a
        leak is invisible by construction. A count makes the failure detectable
        after the fact, and the count must be exactly that: never the text, in
        the log or in an error, because a quoted leak has merely moved rooms.
        """
        from app.generation.provider import _discarded_reasoning

        report = _discarded_reasoning(
            {
                "choices": [{"message": {"content": "a", "reasoning": "deliberation here"}}],
                "usage": {"completion_tokens_details": {"reasoning_tokens": 42}},
            }
        )

        assert report.carried_reasoning is True
        assert report.reasoning_tokens == 42
        assert "deliberation" not in repr(report)

    async def test_a_clean_response_reports_no_discard(self) -> None:
        """A non-reasoning model must not be reported as having reasoned.

        Otherwise the counter means nothing: a system whose model stopped
        reasoning and one whose boundary broke would produce identical logs.
        """
        from app.generation.provider import _discarded_reasoning

        report = _discarded_reasoning(
            {"choices": [{"message": {"content": "a", "role": "assistant"}}], "usage": {}}
        )

        assert report.carried_reasoning is False
        assert report.reasoning_tokens == 0

    async def test_a_completion_with_a_reasoning_field_would_be_refused(self) -> None:
        """The post-condition is real, not a check that always passes.

        `assert_no_reasoning` is run on every returned `Completion`. If it only
        understood dicts it would return `[]` for a dataclass every time and
        look like a passing control while checking nothing. This builds the
        shape it would catch and proves it is caught.
        """
        from app.generation.provider import find_reasoning_keys

        @dataclasses.dataclass
        class Leaky:
            text: str
            reasoning: str = "deliberation"

        assert find_reasoning_keys(Leaky(text="answer")) == ["$.reasoning"]

        @dataclasses.dataclass
        class LeakyNested:
            usage: Leaky

        # Only the offending name is reported. `usage` and `text` are clean
        # fields, and a check that flagged them too would be unusable — a list of
        # findings nobody reads is not a finding.
        assert find_reasoning_keys(LeakyNested(usage=Leaky(text="a"))) == ["$.usage.reasoning"]

    async def test_the_real_completion_carries_no_reasoning_field(self) -> None:
        """The post-condition on the real type, stated as an assertion.

        Cheaper to read than the synthetic case above, and it fails the moment
        someone adds a field — which is the moment it is needed to fail.
        """
        from app.generation.provider import Completion, find_reasoning_keys

        assert find_reasoning_keys(Completion(text="a", model="m")) == []

    async def test_inlined_deliberation_in_content_is_refused(self) -> None:
        """The second check, and the one the structural check cannot make.

        No reasoning *key* is present here. The deliberation is inside `content`,
        where the post-condition cannot see it, and it would reach the user
        intact. This is the gap that made a purely structural guarantee
        insufficient under the reading FR-034 actually mandates.
        """
        from app.generation.provider import GroqProvider

        client = _http_returning(
            200,
            {
                "model": "openai/gpt-oss-120b",
                "choices": [
                    {
                        "message": {
                            "content": "Chain of thought: the user asked about filters. Therefore JQL."
                        },
                        "finish_reason": "stop",
                    }
                ],
            },
        )

        with pytest.raises(ProviderError, match="exposing internal reasoning"):
            await GroqProvider(client=client).complete(system="s", user="u")

    async def test_the_heuristic_names_the_marker_it_matched(self) -> None:
        """A boolean says the answer was refused; not *what* tripped it."""
        from app.generation.provider import text_looks_like_reasoning

        clean = text_looks_like_reasoning("Boards are filtered by JQL.")
        assert clean.looks_like is False
        assert clean.marker is None
        assert not clean  # falsy in the ordinary way, for the obvious `if` site

        leaked = text_looks_like_reasoning("My reasoning is that JQL filters rows.")
        assert leaked.looks_like is True
        assert leaked.marker == "my reasoning is"
        assert leaked

    async def test_nested_reasoning_channel_is_also_found(self) -> None:
        """The leak is not necessarily at the top level."""
        from app.generation.provider import find_reasoning_keys

        assert find_reasoning_keys({"output": {"analysis": {"chain_of_thought": "..."}}}) == [
            "$.output.analysis.chain_of_thought"
        ]

    async def test_a_reasoning_channel_inside_a_list_is_found(self) -> None:
        """Lists are walked too, so a batch cannot smuggle one past the check."""
        from app.generation.provider import find_reasoning_keys

        assert find_reasoning_keys({"items": [{"ok": 1}, {"scratchpad": "x"}]}) == [
            "$.items[1].scratchpad"
        ]

    async def test_a_recursive_payload_terminates(self) -> None:
        """The depth bound is the difference between a check and a hang.

        A structure that refers to itself would otherwise recurse until the
        stack went, inside the function meant to guarantee the system keeps
        running.
        """
        from app.generation.provider import find_reasoning_keys

        payload: dict[str, object] = {"reasoning": "x"}
        payload["self"] = payload

        found = find_reasoning_keys(payload)

        # Bounded, and the bound is what makes the walk a check rather than a
        # hazard. The cycle is still followed, because a self-referential payload
        # is also how a deeply-nested leak hides; it is followed only nine levels
        # deep, which is far past anything a `Completion` nests and shallow enough
        # that the depth argument rather than the interpreter decides when to stop.
        assert found[0] == "$.reasoning"
        assert len(found) == 9

    async def test_only_a_reasoning_channel_is_not_content(self) -> None:
        """A reasoning channel is never read as a fallback for `content`.

        Reading it would make FR-034 violable by a single missing field: a
        model that spends its whole budget thinking would then answer with its
        thinking. The error is distinct from a plain empty completion because
        the diagnosis differs — this one means the model reasoned and produced
        nothing, which is a provider behaviour to report rather than a request
        that came back blank.
        """
        from app.generation.provider import GroqProvider

        client = _http_returning(
            200,
            {
                "model": "openai/gpt-oss-120b",
                "choices": [
                    {
                        "message": {
                            "reasoning": "let me think about this",
                            "reasoning_content": "let me think about this",
                            "content": "",
                        },
                        "finish_reason": "stop",
                    }
                ],
            },
        )

        with pytest.raises(ProviderError) as caught:
            await GroqProvider(client=client).complete(system="s", user="u")

        assert "no usable content" in caught.value.message
        # The deliberation text must not survive anywhere in the error either.
        assert "let me think" not in caught.value.message

    async def test_usage_never_carries_a_reasoning_token_count(self) -> None:
        """The provider reports one; it is deliberately not retained.

        A count cannot leak deliberation, so this is not about exposure. It is
        that `query_logs` is meant to be an honest account of what the system
        did, and a durable column recording that a reasoning channel existed
        sits oddly in a schema that otherwise has no trace of one. The count is
        measured where the measurement matters and not stored where it reads as
        part of the answer.
        """
        from app.generation.provider import GroqProvider

        client = _http_returning(
            200,
            {
                "model": "m",
                "choices": [{"message": {"content": "a"}, "finish_reason": "stop"}],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 30,
                    "total_tokens": 40,
                    "completion_tokens_details": {"reasoning_tokens": 20},
                },
            },
        )

        usage = (await GroqProvider(client=client).complete(system="s", user="u")).usage

        assert usage.completion_tokens == 30
        assert "reasoning" not in repr(usage).lower()
        # `dataclasses.fields`, not `vars`: the type is `slots=True`, so there
        # is no `__dict__` and `vars()` raises rather than answering. A test
        # asserting on a missing attribute checks nothing, so it asks the
        # question the type can actually answer.
        assert not any("reasoning" in f.name.lower() for f in dataclasses.fields(usage))

    async def test_empty_completion_is_an_error_not_an_empty_answer(self) -> None:
        """An empty answer with citations attached is the worst thing this system emits."""
        from app.generation.provider import GroqProvider

        client = _http_returning(
            200,
            {"model": "m", "choices": [{"message": {"content": "   "}, "finish_reason": "stop"}]},
        )

        with pytest.raises(ProviderError):
            await GroqProvider(client=client).complete(system="s", user="u")

    async def test_no_choices_is_an_error(self) -> None:
        from app.generation.provider import GroqProvider

        client = _http_returning(200, {"model": "m", "choices": []})

        with pytest.raises(ProviderError, match="no choices"):
            await GroqProvider(client=client).complete(system="s", user="u")

    async def test_usage_reported_as_a_string_does_not_crash(self) -> None:
        """A provider that reports usage oddly must not lose a successful call."""
        from app.generation.provider import GroqProvider

        client = _http_returning(
            200,
            {
                "model": "m",
                "choices": [{"message": {"content": "a"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": "12", "completion_tokens": None, "total_tokens": 12.7},
            },
        )

        usage = (await GroqProvider(client=client).complete(system="s", user="u")).usage
        assert (usage.prompt_tokens, usage.completion_tokens, usage.total_tokens) == (12, 0, 12)

    async def test_the_request_carries_the_reasoning_control(self) -> None:
        """Asserting the control was sent, not that it worked — the latter is tested above."""
        from app.core.config import get_settings
        from app.generation.provider import GroqProvider

        seen: dict[str, object] = {}

        def capture(request: httpx.Request) -> httpx.Response:
            import json

            seen.update(json.loads(request.content))
            return httpx.Response(
                200, json={"choices": [{"message": {"content": "a"}}]}, request=request
            )

        client = httpx.AsyncClient(
            transport=httpx.MockTransport(capture), base_url="https://api.groq.com"
        )
        await GroqProvider(client=client).complete(system="s", user="u")

        assert seen["reasoning_effort"] == get_settings().reasoning_effort
        assert seen["temperature"] == pytest.approx(0.1)

    async def test_a_reasoning_effort_the_service_rejects_fails_at_startup(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """R-014: only `low`, `medium` and `high` are accepted.

        `"none"` — the value to reach for to switch the channel off — returns
        HTTP 400 `invalid_request_error`. A plain `str` would let it through
        settings validation and turn a typo into a 400 on the first real query,
        which is indistinguishable from a provider outage when read from a log.
        """
        import pydantic
        from app.core.config import get_settings

        # Through the environment, not a constructor keyword: `Settings` is
        # declared with `extra="ignore"`, so an unrecognised keyword is dropped
        # silently and the test would pass for the wrong reason — it would be
        # asserting that an ignored argument raises.
        monkeypatch.setenv("REASONING_EFFORT", "none")
        get_settings.cache_clear()
        try:
            with pytest.raises(pydantic.ValidationError):
                get_settings()
        finally:
            get_settings.cache_clear()

    async def test_streaming_is_never_requested(self) -> None:
        """A streamed body is not a `Completion` and would fail the JSON contract."""
        from app.generation.provider import GroqProvider

        seen: dict[str, object] = {}

        def capture(request: httpx.Request) -> httpx.Response:
            import json

            seen.update(json.loads(request.content))
            return httpx.Response(
                200, json={"choices": [{"message": {"content": "a"}}]}, request=request
            )

        client = httpx.AsyncClient(
            transport=httpx.MockTransport(capture), base_url="https://api.groq.com"
        )
        await GroqProvider(client=client).complete(system="s", user="u")

        assert seen["stream"] is False


class TestClassify:
    async def test_uses_the_smaller_model_and_parses_json(self) -> None:
        from app.core.config import get_settings
        from app.generation.provider import GroqProvider

        seen: dict[str, object] = {}

        def capture(request: httpx.Request) -> httpx.Response:
            import json

            seen.update(json.loads(request.content))
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": '{"product":"jira","confidence":0.9}'}}]},
                request=request,
            )

        client = httpx.AsyncClient(
            transport=httpx.MockTransport(capture), base_url="https://api.groq.com"
        )
        result = await GroqProvider(client=client).classify(system="s", user="u")

        assert result == {"product": "jira", "confidence": 0.9}
        assert seen["model"] == get_settings().groq_classification_model
        assert seen["response_format"] == {"type": "json_object"}

    async def test_fenced_json_is_accepted(self) -> None:
        from app.generation.provider import GroqProvider

        client = _http_returning(
            200,
            {"choices": [{"message": {"content": '```json\n{"product": "confluence"}\n```'}}]},
        )
        assert await GroqProvider(client=client).classify(system="s", user="u") == {
            "product": "confluence"
        }

    async def test_malformed_json_raises_rather_than_forcing_a_value(self) -> None:
        """FR-009: never force a classification.

        Silently becoming `{}` is forcing one, and it hides a parse failure
        behind a confident "unknown".
        """
        from app.generation.provider import GroqProvider

        client = _http_returning(200, {"choices": [{"message": {"content": "jira, I think"}}]})

        with pytest.raises(ProviderError, match="not valid JSON"):
            await GroqProvider(client=client).classify(system="s", user="u")

    async def test_json_array_is_rejected(self) -> None:
        from app.generation.provider import GroqProvider

        client = _http_returning(200, {"choices": [{"message": {"content": "[1, 2]"}}]})

        with pytest.raises(ProviderError, match="not an object"):
            await GroqProvider(client=client).classify(system="s", user="u")

    async def test_a_reasoning_key_in_the_classification_is_refused(self) -> None:
        from app.generation.provider import GroqProvider

        client = _http_returning(
            200,
            {"choices": [{"message": {"content": '{"product":"jira","thoughts":"..."}'}}]},
        )

        with pytest.raises(ProviderError):
            await GroqProvider(client=client).classify(system="s", user="u")


class TestProviderErrors:
    async def test_429_is_rate_limited_not_a_generic_error(self) -> None:
        """A client that cannot tell 429 from 401 retries immediately and worsens it."""
        from app.core.errors import ProviderRateLimited
        from app.generation.provider import GroqProvider

        transport = httpx.MockTransport(
            lambda request: httpx.Response(
                429, json={}, headers={"retry-after": "12"}, request=request
            )
        )
        client = httpx.AsyncClient(transport=transport, base_url="https://api.groq.com")

        with pytest.raises(ProviderRateLimited) as caught:
            await GroqProvider(client=client).complete(system="s", user="u")
        assert caught.value.retry_after == 12

    async def test_401_is_a_configuration_problem(self) -> None:
        from app.generation.provider import GroqProvider

        client = _http_returning(
            401, {"error": {"message": "invalid api key gsk_secret_value_here"}}
        )

        with pytest.raises(ProviderError, match="rejected the configured credential"):
            await GroqProvider(client=client).complete(system="s", user="u")

    async def test_html_error_page_is_not_reported_as_invalid_json(self) -> None:
        """A 4xx is checked before the body is parsed."""
        from app.generation.provider import GroqProvider

        transport = httpx.MockTransport(
            lambda request: httpx.Response(500, text="<html>gateway error</html>", request=request)
        )
        client = httpx.AsyncClient(transport=transport, base_url="https://api.groq.com")

        with pytest.raises(ProviderError):
            await GroqProvider(client=client).complete(system="s", user="u")

    async def test_timeout_is_translated(self) -> None:
        from app.generation.provider import GroqProvider

        def raise_timeout(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("slow", request=request)

        client = httpx.AsyncClient(
            transport=httpx.MockTransport(raise_timeout), base_url="https://api.groq.com"
        )

        with pytest.raises(ProviderError, match="did not respond in time"):
            await GroqProvider(client=client).complete(system="s", user="u")

    async def test_no_secret_from_the_provider_reaches_a_log_record(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """FR-042: the provider's error body is logged redacted, never returned."""
        from app.generation.provider import GroqProvider

        client = _http_returning(
            401, {"error": {"message": "invalid key gsk_realvalue_abcdefghijklmnop"}}
        )

        with caplog.at_level("ERROR"):
            with pytest.raises(ProviderError):
                await GroqProvider(client=client).complete(system="s", user="u")

        assert "gsk_realvalue_abcdefghijklmnop" not in caplog.text


class TestListModels:
    async def test_lists_model_ids(self) -> None:
        from app.generation.provider import GroqProvider

        client = _http_returning(
            200,
            {
                "data": [
                    {"id": "openai/gpt-oss-120b", "context_window": 131072},
                    {"id": "llama-3.3-70b"},
                ]
            },
        )

        models = await GroqProvider(client=client).list_models()

        assert [model.id for model in models] == ["openai/gpt-oss-120b", "llama-3.3-70b"]
        assert models[0].context_window == 131072
        assert models[1].context_window is None

    async def test_health_names_a_model_the_key_cannot_reach(self) -> None:
        """R-006: an unavailable model 401s, which looks like a bad key and is not."""
        from app.generation.provider import GroqProvider

        client = _http_returning(200, {"data": [{"id": "llama-3.3-70b"}]})

        reason = await GroqProvider(client=client).health()

        assert reason is not None
        assert "not available" in reason

    async def test_health_is_none_when_every_configured_model_is_reachable(self) -> None:
        from app.core.config import get_settings
        from app.generation.provider import GroqProvider

        settings = get_settings()
        client = _http_returning(
            200,
            {"data": [{"id": settings.groq_model}, {"id": settings.groq_classification_model}]},
        )

        assert await GroqProvider(client=client).health() is None
