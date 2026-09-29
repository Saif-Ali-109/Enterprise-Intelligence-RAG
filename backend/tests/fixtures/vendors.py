"""Test doubles for the vendor SDKs.

**The fakes are built from the real SDK response types, not from hand-rolled
stubs.** This is the single most important decision in the test suite and it was
made after a bug proved its worth: `vector_store._hits_from` originally assumed
`search().result` was a list of per-namespace blocks, because that is what a
plausible-looking stub would be given. The real `pinecone==10.0.0` type is
`SearchResult` with exactly one field, `hits`, so the original code raised
`TypeError` on every real response.

A stub written by the same person who wrote the code under test reproduces that
person's assumptions. Constructing `SearchRecordsResponse(result=SearchResult(
hits=[Hit(id_=..., score_=...)]), usage=...)` cannot: the real type is the
source of truth, so any attribute the adapter reaches for is either present or
an `AttributeError` at test time. Every assertion about SDK response shape in
this suite is therefore a real assertion, not a tautology.

Two things the fakes deliberately do NOT do:

**They do not simulate vendor failure modes.** `PineconeConnectionError`,
`429`, and malformed responses are the cases where the adapter's error
translation matters most, and a fake that invents its own exception shapes would
test the fake. The error paths are covered instead by injecting the SDK's real
exception classes, which is what `test_vendor_adapters.py` does.

**They do not make assertions about the network.** Nothing here pretends to have
talked to Pinecone. A test that passes against a fake and is marked
`integration` would be a lie about what was verified; the real ones are in
`tests/integration/` and skip loudly when a key is absent.
"""

from __future__ import annotations

from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Pinecone response builders
# ---------------------------------------------------------------------------


def make_search_response(
    hits: list[tuple[str, float]],
    *,
    fields: dict[str, dict[str, Any]] | None = None,
) -> Any:
    """Build a real `SearchRecordsResponse`.

    `hits` is `(id, score)` pairs, which is all the adapter is supposed to read.
    `fields` lets a test attach record data, for the case where the service
    returns fields the caller did not ask for — the empty-`fields` assertion in
    T027's counterpart test depends on being able to represent that.
    """
    from pinecone import SearchRecordsResponse
    from pinecone.models.vectors.search import SearchResult, SearchUsage

    record_fields = fields or {}
    return SearchRecordsResponse(
        result=SearchResult(
            hits=[
                # `Hit` takes `id_`/`score_` and renames them to `_id`/`_score` on
                # the wire. Constructing with the underscored names is the only
                # way to build one, and it is deliberate: the adapter must read
                # `hit.id` and `hit.score`, the properties, and would fail here
                # if it reached for the wire names.
                _make_hit(identifier, score, record_fields.get(identifier, {}))
                for identifier, score in hits
            ]
        ),
        usage=SearchUsage(read_units=len(hits), embed_total_tokens=0, rerank_units=0),
    )


def _make_hit(identifier: str, score: float, fields: dict[str, Any]) -> Any:
    from pinecone import Hit

    return Hit(id_=identifier, score_=score, fields=fields)


def make_empty_search_response() -> Any:
    """A search that matched nothing. The SDK returns empty hits, not an error."""
    return make_search_response([])


def make_rerank_result(
    pairs: list[tuple[int, float]],
    *,
    model: str = "bge-reranker-v2-m3",
) -> Any:
    """Build a real `RerankResult` from `(index, score)` pairs.

    `document` is left `None`, which is what the service returns when
    `return_documents=False` — the setting this codebase uses. A fake that
    returned documents would let the adapter appear to work by reading an id out
    of the returned text, which is exactly the mistake the real service makes
    impossible.
    """
    from pinecone import RerankResult
    from pinecone.models.inference.rerank import RankedDocument, RerankUsage

    return RerankResult(
        model=model,
        data=[RankedDocument(index=index, score=score, document=None) for index, score in pairs],
        usage=RerankUsage(rerank_units=len(pairs)),
    )


def make_index_model(
    *,
    name: str = "enterprise-knowledge",
    ready: bool = True,
    state: str = "Ready",
    model: str = "llama-text-embed-v2",
    metric: str | None = "cosine",
    read_input_type: str | None = "query",
    write_input_type: str | None = "passage",
    field_name: str = "chunk_text",
) -> Any:
    """Build a real `IndexModel` for an integrated-embedding index.

    The shape here is the one confirmed by introspecting `pinecone==10.0.0`, and
    it is not what the older documentation describes. `IndexModel` in that
    version has **no** `spec`, **no** `dimension`, and **no** string `status`:

        IndexModel(name, status, schema, deployment, deletion_protection, ...)

    - `status` is an `IndexStatus(ready: bool, state: str)`. Readiness is the
      bool; the state is the label. An adapter that stringifies `status` and
      compares it to `"Ready"` reports every index as not ready, forever.
    - `schema` is an `IndexSchema(fields: dict[str, IndexSchemaField])` and is
      where the index's shape lives. For integrated embedding the field is a
      `SemanticTextField` carrying `model` and the `read_parameters` /
      `write_parameters` that hold `input_type`.
    - `deployment` is a `ManagedDeployment(cloud, region, environment)`.

    The `SemanticTextField` is the reason this function takes `read_input_type`
    and `write_input_type` arguments. Pinecone documents that an `input_type`
    mismatch "quietly degrades" search quality while both calls succeed, so a
    fake that always returned the correct value would make that check untestable
    — and the check is the only thing standing between a misconfigured index and
    silently worse answers.
    """
    from pinecone import IndexModel, IndexSchema, IndexStatus
    from pinecone.models.indexes.deployment import ManagedDeployment

    field: Any
    if model is None:
        # The manual-embedding shape: a dense_vector field with no `model`.
        # Used to prove `describe()` reports `embed_model: None` rather than
        # falling back to the configured name and claiming a check it could not
        # actually perform.
        from pinecone import DenseVectorField

        field = DenseVectorField(dimension=1024, metric=metric or "cosine", description=None)
    else:
        from pinecone import SemanticTextField

        field = SemanticTextField(
            model=model,
            metric=metric,
            description=None,
            read_parameters={"input_type": read_input_type, "truncate": "END"}
            if read_input_type
            else {},
            write_parameters={"input_type": write_input_type, "truncate": "END"}
            if write_input_type
            else {},
        )

    return IndexModel(
        name=name,
        status=IndexStatus(ready=ready, state=state),
        schema=IndexSchema(fields={field_name: field}),
        deployment=ManagedDeployment(cloud="aws", region="us-east-1", environment=None),
        deletion_protection="disabled",
        host=f"{name}.svc.pinecone.io",
    )


# ---------------------------------------------------------------------------
# Fake clients
# ---------------------------------------------------------------------------


class FakePineconeIndex:
    """Stands in for `Pinecone.Index`.

    Records the arguments it was called with, so a test can assert on the
    *request* — that `inputs` was `{"text": ...}`, that `filter` was a dict and
    not an interpolated string, that no `dimension` was sent — which is where
    most of this adapter's real risk lives.
    """

    def __init__(
        self,
        *,
        search_response: Any | None = None,
        upsert_results: list[Any] | None = None,
        deleted: list[str] | None = None,
    ) -> None:
        self.search_response = search_response
        self.upsert_results = upsert_results
        self.deleted: list[list[str]] = []

        self.search_calls: list[dict[str, Any]] = []
        self.upsert_calls: list[dict[str, Any]] = []
        self.delete_calls: list[dict[str, Any]] = []
        self.stats_response: Any = None

    def search(self, **kwargs: Any) -> Any:
        self.search_calls.append(kwargs)
        if self.search_response is None:
            return make_empty_search_response()
        return self.search_response

    def upsert(self, **kwargs: Any) -> Any:
        self.upsert_calls.append(kwargs)
        if self.upsert_results:
            return self.upsert_results.pop(0)
        return {"upsertedCount": len(kwargs.get("vectors", []))}

    def delete(self, **kwargs: Any) -> Any:
        self.delete_calls.append(kwargs)
        self.deleted.append(list(kwargs.get("ids", [])))
        return {}

    def delete_namespace(self, **kwargs: Any) -> Any:
        self.delete_calls.append(kwargs)
        return {}

    def describe_index_stats(self) -> Any:
        return self.stats_response

    def fetch(self, **kwargs: Any) -> Any:
        return {"vectors": {}}

    # -- the attribute the adapter must never ask for -----------------------

    def __getattr__(self, name: str) -> Any:
        # An unrecognised attribute on a *real* SDK object would be an
        # AttributeError. Raising one here keeps that behaviour, so an adapter
        # reaching for a method that does not exist fails here too.
        raise AttributeError(f"FakePineconeIndex has no attribute {name!r}")


class FakeInference:
    """Stands in for `Pinecone.inference`."""

    def __init__(self, result: Any | None = None) -> None:
        self.result = result
        self.rerank_calls: list[dict[str, Any]] = []

    def rerank(self, **kwargs: Any) -> Any:
        self.rerank_calls.append(kwargs)
        if self.result is not None:
            return self.result
        # Default: score the documents in reverse, which makes a mapping error
        # between `index` and candidate position visible rather than invisible.
        count = len(kwargs.get("documents", []))
        return make_rerank_result([(count - 1 - i, 0.5) for i in range(count)])


class FakeIndexes:
    """Stands in for `Pinecone.indexes`.

    The split into a control-plane object and a data-plane `Index` handle
    mirrors the real client in 10.0.0, where `pc.indexes.describe()` and
    `pc.Index(name)` are different objects reached from the same client. An
    adapter written against a single flat fake would not catch a call made on
    the wrong one.
    """

    def __init__(self, client: FakePineconeClient) -> None:
        self._client = client

    def exists(self, name: str) -> bool:
        self._client.exists_calls.append(name)
        return self._client._exists  # noqa: SLF001 - same module, same fake

    def describe(self, name: str) -> Any:
        self._client.describe_calls.append(name)
        return self._client._model  # noqa: SLF001

    def create_for_model(self, **kwargs: Any) -> Any:
        self._client.created.append(kwargs)
        self._client._exists = True  # noqa: SLF001
        return self._client._model  # noqa: SLF001

    def create(self, **kwargs: Any) -> Any:
        """Present so a test asserting the adapter does NOT use the manual path fails loudly."""
        self._client.created.append({"__manual_create__": kwargs})
        self._client._exists = True  # noqa: SLF001
        return self._client._model  # noqa: SLF001

    def __getattr__(self, name: str) -> Any:
        raise AttributeError(f"FakeIndexes has no attribute {name!r}")


class FakePineconeClient:
    """Stands in for `Pinecone`.

    The legacy shims (`has_index`, `create_index_for_model`) are **deliberately
    absent**. Both exist in the real 10.0.0 client and both are documented as
    backwards-compatibility shims whose own docstrings say new code should use
    `indexes.exists` and `indexes.create_for_model`. Omitting them here means an
    adapter that reaches for a shim fails in tests rather than working until
    Pinecone drops the shim in a future release.
    """

    def __init__(
        self,
        *,
        index: FakePineconeIndex | None = None,
        inference: FakeInference | None = None,
        index_exists: bool = True,
        index_model: Any | None = None,
    ) -> None:
        self.index_handle = index or FakePineconeIndex()
        self.inference = inference or FakeInference()
        self._exists = index_exists
        self._model = index_model or make_index_model()

        self.indexes = FakeIndexes(self)

        self.created: list[dict[str, Any]] = []
        self.exists_calls: list[str] = []
        self.describe_calls: list[str] = []

    def Index(self, name: str = "", host: str = "", **kwargs: Any) -> FakePineconeIndex:  # noqa: N802
        """The capital-I shim. Present so reaching for it fails loudly.

        `Pinecone.Index` is documented as a backwards-compatibility shim for
        `Pinecone.index`. The fake implements it so that using it raises a
        targeted failure naming the supported call, rather than an
        `AttributeError` that reads like a missing dependency.
        """
        raise AssertionError(
            "Pinecone.Index is a backwards-compatibility shim in pinecone 10.0.0. "
            "Use client.index(name=...) — see PineconeVectorStore.index."
        )

    def index(self, name: str = "", host: str = "", **kwargs: Any) -> FakePineconeIndex:
        """The data-plane handle. The SDK's supported spelling."""
        return self.index_handle

    def describe_index(self, name: str) -> Any:
        raise AssertionError(
            "Pinecone.describe_index is a backwards-compatibility shim in pinecone 10.0.0. "
            "Use client.indexes.describe(name) — see PineconeVectorStore.describe."
        )

    def __getattr__(self, name: str) -> Any:
        # `has_index` and `create_index_for_model` land here and raise, which is
        # the point: they are real shims upstream but this codebase must not
        # depend on them.
        raise AttributeError(
            f"FakePineconeClient has no attribute {name!r}. Note: legacy shims such as "
            f"has_index/create_index_for_model are intentionally not implemented — use "
            f"indexes.exists / indexes.create_for_model."
        )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_pinecone() -> FakePineconeClient:
    return FakePineconeClient()


@pytest.fixture
def fake_index() -> FakePineconeIndex:
    return FakePineconeIndex()
