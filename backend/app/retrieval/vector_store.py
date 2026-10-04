"""The `VectorStore` interface, and the Pinecone adapter that implements it.

T029. FR-061, FR-062, Principle VII, and the two vendor facts settled during
research: R-001 (the embedding model and its 2048-token ceiling) and R-002
(`create_index_for_model` is the only supported spelling in `pinecone==10.0.0`;
`create_for_model` does not exist).

**The interface exists so the retrieval stages never import Pinecone.** A
`Retriever` that calls `pc.index().search(...)` directly cannot be tested without
a Pinecone account, and cannot be pointed at a different store without rewriting
the stage. Every method here is a protocol, and the pipeline is written against
the protocol. That is the whole of Principle VII for this layer.

**One decision deserves stating up front, because it looks wrong at a glance:
`upsert` takes plain text, not vectors.** Pinecone's integrated index embeds on
write, so the client never sees a vector. Accepting a `list[float]` here and
silently ignoring it would be a trap: a caller could compute embeddings, pay for
the round trip to embed them again server-side, and never find out. Taking text
makes the double-embedding mistake impossible to write.

**`input_type` is asymmetric on purpose: `query` in, `passage` out.** R-001
records that Pinecone's documentation says a mismatch "quietly degrades" search
quality while both calls still succeed. Asymmetry is the documented correct
configuration, and the smoke test in T027 exists to confirm the service honours
it. If a future service version ignores the distinction, retrieval gets quietly
worse and nothing raises — which is why `embedding_config` is reported by
`describe()` rather than being assumed.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from app.core.config import get_settings
from app.core.errors import ProviderError, VectorServiceUnavailable
from app.core.logging import get_logger, redact

_log = get_logger("retrieval.vector_store")


# ============================================================================
# Value objects
# ============================================================================


@dataclass(frozen=True, slots=True)
class VectorRecord:
    """One retrievable unit, as stored.

    `text` is the embedded field. It is present on write and on fetch but is
    **not** returned by a search: a search returns ids and scores, and the
    pipeline joins those ids against `document_units` in the registry. Returning
    the text from the vector store as well would put a second copy of
    third-party content in the answer path with no authority behind it.

    `metadata` is the flat, filterable denormalisation required by
    data-model.md §5. Kept to scalars and string lists: Pinecone's filter
    language has no notion of a nested object, and a nested dict silently fails
    to match, which is a filter that looks configured and is not.
    """

    id: str
    text: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SearchHit:
    """One result. `score` is a similarity from the embed model, in [0, 1].

    Comparable **only** to other hits from the same query and the same model.
    Never averaged with a rerank score: the two come from different models on
    different scales, and contracts/README.md is explicit that the API never
    presents them on a shared scale (FR-021).
    """

    id: str
    score: float
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class IndexInfo:
    """What `describe()` reports about the configured index.

    Every field here is read from a shape that was confirmed by introspecting
    `pinecone==10.0.0`, not from documentation. In that version `IndexModel` has
    no `spec`, no `dimension`, no `metric`, and no string `status`: readiness is
    `status.ready` (a bool), the human-readable state is `status.state`, and the
    index's shape lives in `schema.fields`. The first draft of this adapter read
    `info.spec.metric` and compared `str(info.status).lower() == "ready"`, which
    would have reported every index as not ready forever.

    `input_type` is the field that earns this dataclass its place. Pinecone
    documents that an `input_type` mismatch "quietly degrades" search quality
    while both calls still succeed, and `input_type` is fixed at index creation.
    So it is read back from the service on every `describe()` and compared with
    what this application intends, turning a risk that is otherwise invisible
    into a health-check finding (R-001).

    `expected_dimension` is `None` by design. On the integrated-embedding path
    this application never has an opinion about the dimension — the service sets
    it — so there is nothing to compare against, and inventing an expected value
    would recreate the hard-coding Principle VII forbids.
    """

    name: str
    ready: bool
    state: str
    embed_model: str | None = None
    metric: str | None = None
    dimension: int | None = None
    namespace: str | None = None
    read_input_type: str | None = None
    write_input_type: str | None = None
    expected_read_input_type: str = "query"
    expected_write_input_type: str = "passage"

    @property
    def status(self) -> str:
        """The service's state label. Kept as the contract's `status` string."""
        return self.state

    @property
    def expected_dimension(self) -> None:
        """Always `None`: the service owns the dimension (Principle VII, R-002)."""
        return None

    def input_type_mismatch(self) -> str | None:
        """Describe an `input_type` divergence, or `None` when it is correct.

        Returns a phrase rather than a bool so the health probe can report *what*
        is wrong. "Degraded" with no detail is the kind of status nobody can act
        on, and this is the single most valuable thing `/health` can say about
        the vector service.
        """
        problems: list[str] = []
        if (
            self.read_input_type is not None
            and self.read_input_type != self.expected_read_input_type
        ):
            problems.append(
                f"reads embed as {self.read_input_type!r}, expected {self.expected_read_input_type!r}"
            )
        if (
            self.write_input_type is not None
            and self.write_input_type != self.expected_write_input_type
        ):
            problems.append(
                f"writes embed as {self.write_input_type!r}, expected {self.expected_write_input_type!r}"
            )
        if not problems:
            return None
        return (
            "Index embedding input_type does not match this application's configuration ("
            + "; ".join(problems)
            + "). Search quality degrades silently while every call still succeeds."
        )


@dataclass(frozen=True, slots=True)
class BatchResult:
    """The outcome of a batch write.

    `written` is the count the service acknowledged, not the count submitted. A
    caller that trusts the submitted count will later believe a chunk is
    retrievable when it was silently dropped, and the failure surfaces as an
    unanswerable question with no ingestion error to explain it.
    """

    written: int
    submitted: int


# ============================================================================
# The interface
# ============================================================================


@runtime_checkable
class VectorStore(Protocol):
    """The operations retrieval and ingestion need. Implemented by Pinecone.

    `Protocol` with `runtime_checkable` rather than an ABC: the test suite
    supplies fakes that satisfy the shape without inheriting anything, and a
    fake that forgets a method fails at the call site rather than at
    registration — which is a better place to find out.
    """

    async def describe(self) -> IndexInfo:
        """The index's current state. Must not write, and must not cost money."""
        ...

    async def upsert(
        self, records: list[VectorRecord], *, namespace: str | None = None
    ) -> BatchResult:
        """Write records. The store embeds; callers pass text."""
        ...

    async def search(
        self,
        *,
        query: str,
        top_k: int,
        namespace: str | None = None,
        metadata_filter: dict[str, Any] | None = None,
    ) -> list[SearchHit]:
        """Nearest neighbours for a text query. The query is embedded with `input_type=query`."""
        ...

    async def delete(self, ids: list[str], *, namespace: str | None = None) -> int:
        """Delete by id. Returns the number the service acknowledged."""
        ...

    async def delete_namespace(self, *, namespace: str | None = None) -> None:
        """Delete an entire namespace. Used only by a full re-index."""
        ...

    async def fetch(self, ids: list[str], *, namespace: str | None = None) -> list[VectorRecord]:
        """Fetch records by id, including their text and metadata."""
        ...


# ============================================================================
# Pinecone adapter
# ============================================================================


def _to_metadata_filter(metadata_filter: dict[str, Any] | None) -> dict[str, Any] | None:
    """Convert a plain dict to Pinecone's filter expression.

    A dict is already the right shape: `{"product": "jira"}` is a valid
    Pinecone filter, and `{"$in": [...]}` handles the multi-value case. So no
    expression language is built here, and no string interpolation into a filter
    expression happens anywhere — which is the SSRF-class bug that filter
    languages invite and the reason the conversion is a pass-through.

    `None` is passed through as `None` rather than `{}`, because Pinecone treats
    an empty filter and no filter differently in ways that have changed between
    versions, and `None` is the unambiguous "no filter".
    """
    if not metadata_filter:
        return None
    return {key: value for key, value in metadata_filter.items() if value is not None}


class PineconeVectorStore:
    """`VectorStore` over Pinecone's integrated-embedding serverless index.

    Every vendor call is wrapped: the SDK is synchronous and blocking, and
    calling it directly from an async handler blocks the event loop for the
    duration of a network round trip — which for this system is the difference
    between serving one question and serving none while the corpus indexes.

    The wrapping is `asyncio.to_thread`, not a rewrite against the async SDK.
    `PineconeAsyncio` exists in 10.0.0 but is a different client with a different
    surface, and adopting it would mean the vector adapter and the rest of the
    codebase use two SDK idioms. The thread offload is contained to this one
    class.
    """

    def __init__(
        self,
        *,
        client: Any | None = None,
        index_name: str | None = None,
        namespace: str | None = None,
    ) -> None:
        settings = get_settings()
        self._client = client
        self._index_name = index_name or settings.pinecone_index_name
        self._namespace = namespace or settings.pinecone_namespace
        self._embed_model = settings.pinecone_embed_model
        self._index: Any | None = None

    # -- client lifecycle --------------------------------------------------

    @property
    def client(self) -> Any:
        """The Pinecone client, built on first use.

        Built lazily rather than in `__init__` so importing this module does not
        construct a client: a test that imports the retriever should not need a
        key, and `Settings` already refuses to start without one, so an
        import-time client would make the whole package unimportable in a test
        that has set a dummy key and wants a fake.
        """
        if self._client is None:
            from pinecone import Pinecone

            settings = get_settings()
            try:
                self._client = Pinecone(
                    api_key=settings.pinecone_api_key.get_secret_value(),
                    source_tag="enterprise-knowledge-rag",
                )
            except Exception as exc:  # noqa: BLE001
                # A missing or malformed key is a configuration error, and it is
                # reported as one rather than as a vector outage — the two have
                # opposite remedies and the operator needs to be told which.
                raise ProviderError(
                    "The vector service client could not be constructed. Check PINECONE_API_KEY."
                ) from exc
        return self._client

    @property
    def index(self) -> Any:
        """The data-plane index handle, built on first use.

        `client.index(...)`, lowercase. `client.Index(...)` with a capital I
        still exists in `pinecone==10.0.0` but its own docstring reads
        "Backwards-compatibility shim for `Pinecone.index`… New code should use
        `pc.index()` instead", so this calls the method the SDK asks new code to
        call. Depending on a shim is a scheduled outage: these are documented as
        existing to ease migration off the 9.x SDK, and the 9.x surface is
        already gone (the 9.x `spec=IntegratedSpec(...)` route now raises
        `PineconeTypeError`).
        """
        if self._index is None:
            try:
                self._index = self.client.index(name=self._index_name)
            except Exception as exc:  # noqa: BLE001
                raise VectorServiceUnavailable() from exc
        return self._index

    @property
    def namespace(self) -> str:
        return self._namespace

    @property
    def index_name(self) -> str:
        """The configured index name. Public because `ensure_index` needs it."""
        return self._index_name

    @property
    def embed_model(self) -> str:
        """The configured embedding model name. Reported by `/config`."""
        return self._embed_model

    # -- error translation -------------------------------------------------

    def _translate(self, exc: Exception, operation: str) -> Exception:
        """Map a vendor exception to this system's error vocabulary.

        Two distinctions are preserved because they have different remedies:

        - **401/403** means the credential is wrong or not entitled. That is a
          configuration problem, and `PROVIDER_ERROR` is the honest code.
        - **Connection failure / timeout** means the service is unreachable.
          That is `VECTOR_SERVICE_UNAVAILABLE`, and per FR-061/FR-062 it is
          terminal — no fallback, no cached answer, no degraded response.

        Collapsing both into one code would leave an operator reading "vector
        service unreachable" when the actual problem is a key that lost its
        entitlement for a model.
        """
        from pinecone import PineconeApiException, PineconeConnectionError, PineconeTimeoutError

        name = type(exc).__name__
        detail = redact(str(exc))

        if isinstance(exc, (PineconeConnectionError, PineconeTimeoutError)):
            _log.error(
                "vector service unreachable", extra={"operation": operation, "error": detail}
            )
            return VectorServiceUnavailable()

        if isinstance(exc, PineconeApiException):
            # `status_code`, not `status`. The latter is not an attribute of
            # `ApiError` in pinecone==10.0.0, so reading it returned `None` on
            # every call and the 401/403 branch below was unreachable — the
            # exact branch the docstring claims to implement.
            status = getattr(exc, "status_code", None)
            _log.error(
                "vector service rejected a call",
                extra={"operation": operation, "status": status, "error": detail},
            )
            if status in (401, 403):
                return ProviderError("The vector service rejected the configured credential.")
            return ProviderError()

        # A *plain* `ApiError` is the parent of `PineconeApiException`, not a
        # sibling, so this branch catches dimension mismatches and other bare
        # service rejections. T027 hit exactly this: an upsert sent the wrong
        # shape came back as
        # `ApiError: [400] Vector dimension 1 does not match the dimension of
        # the index 1024`, which the branch above does not catch. It fell through
        # to the return below and inherited `ProviderError`'s default message —
        # "The language model provider failed" — sending an operator to debug
        # Groq when the fault was a 1024-versus-1 vector in this application's
        # own write path.
        #
        # `VectorServiceUnavailable` rather than `ProviderError` because the
        # remedy differs: this is a request the vector service understood and
        # refused, not a credential and not a language model. The messages are
        # named at each return for the same reason — telling three faults apart
        # is the entire job of this function, and a default message identifies
        # none of them.
        _log.error(
            "vector service call failed",
            extra={"operation": operation, "error_type": name, "error": detail},
        )
        return VectorServiceUnavailable()

    async def _call(self, operation: str, func: Any, /, *args: Any, **kwargs: Any) -> Any:
        """Run a blocking SDK call off the event loop, translating failures."""
        try:
            return await asyncio.to_thread(func, *args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            raise self._translate(exc, operation) from None

    # -- VectorStore -------------------------------------------------------

    async def describe(self) -> IndexInfo:
        """Report the index's state without writing or paying for an embedding.

        Uses `client.indexes.describe(name)`, not `client.describe_index(name)`.
        Both work in 10.0.0 and the second is documented as a backwards-compatibility
        shim for the first.

        Reads the confirmed 10.0.0 shape: `status.ready` is a bool, `status.state`
        is the label, and the embedding configuration is on the `semantic_text`
        field inside `schema.fields`. Every read is `getattr`-with-default so a
        field a future version drops becomes `None` rather than an
        `AttributeError` inside a health check — the one place an exception is
        least welcome.
        """
        info = await self._call("describe", self.client.indexes.describe, self._index_name)

        raw_status = getattr(info, "status", None)
        ready = bool(getattr(raw_status, "ready", False))
        state = str(getattr(raw_status, "state", "") or "")

        embed_model, metric, dimension, read_params, write_params = self._read_schema(info)

        return IndexInfo(
            name=str(getattr(info, "name", self._index_name) or self._index_name),
            ready=ready,
            state=state or ("Ready" if ready else "unknown"),
            embed_model=embed_model or self._embed_model,
            metric=metric,
            dimension=dimension,
            namespace=self._namespace,
            read_input_type=_parameter(read_params, "input_type"),
            write_input_type=_parameter(write_params, "input_type"),
        )

    @staticmethod
    def _read_schema(info: Any) -> tuple[str | None, str | None, int | None, Any, Any]:
        """Pull the embedding field's configuration out of `IndexModel.schema`.

        A Pinecone 10.0.0 index created for integrated embedding has exactly one
        field of interest, a `semantic_text` field named after the `field_map`
        target. It is found by *type* rather than by name, because the name is
        this application's choice (`chunk_text`) and looking it up by name would
        make `describe()` silently return nothing if the field map were ever
        renamed — the failure would look identical to "the service returned no
        schema".

        Returns `(model, metric, dimension, read_parameters, write_parameters)`.
        """
        schema = getattr(info, "schema", None)
        fields = getattr(schema, "fields", None)
        if not isinstance(fields, dict):
            return (None, None, None, None, None)

        for schema_field in fields.values():
            if getattr(schema_field, "model", None) is None:
                # Not a `semantic_text` field. A `dense_vector` field has no
                # `model`, which is the manual-embedding shape, and this
                # application does not use it.
                continue
            return (
                getattr(schema_field, "model", None),
                getattr(schema_field, "metric", None),
                # `dimension` is not on a semantic_text field; it is on a
                # dense_vector one. `None` here is the honest integrated case.
                getattr(schema_field, "dimension", None),
                getattr(schema_field, "read_parameters", None),
                getattr(schema_field, "write_parameters", None),
            )

        return (None, None, None, None, None)

    async def vector_count(self) -> int | None:
        """How many vectors the namespace holds. Used by the evaluator, not /health."""
        try:
            stats = await self._call("stats", self.index.describe_index_stats)
        except Exception:  # noqa: BLE001
            # A count that cannot be obtained is `None`, not `0`. Reporting 0
            # for an unreachable service is the same class of lie as a health
            # check that returns ok.
            return None

        namespaces = getattr(stats, "namespaces", None) or {}
        entry = namespaces.get(self._namespace) if isinstance(namespaces, dict) else None
        if entry is not None:
            count = getattr(entry, "vector_count", None)
            if isinstance(count, int):
                return count

        total = getattr(stats, "total_vector_count", None)
        return total if isinstance(total, int) else None

    async def upsert(
        self, records: list[VectorRecord], *, namespace: str | None = None
    ) -> BatchResult:
        """Write records; the service embeds each with `input_type=passage`.

        **Uses `upsert_records` with the text, never `upsert` with `values`.**
        This was the first thing T027 measured against a live index, and the
        measurement contradicted the code: sending `values=[0.0]` is rejected
        outright with

            [400] Vector dimension 1 does not match the dimension of the index 1024

        There is no zero-vector placeholder convention on this surface. The
        service embeds from the schema's `semantic_text` field, so the text must
        be handed over under that field's name and the vector must be omitted
        entirely. R-003 said exactly this and the code did not follow it, which
        is the failure mode research notes are supposed to prevent.

        Batched. Pinecone accepts an array of records in one call, and one call
        per chunk would be one HTTP round trip per chunk — for a 100-page corpus
        at 40 chunks per page, four thousand round trips. `UPSERT_BATCH` is a
        service-side bound, not a tuning knob invented here.
        """
        if not records:
            return BatchResult(written=0, submitted=0)

        target_namespace = namespace or self._namespace
        submitted = len(records)
        written = 0

        # `UPSERT_BATCH` deliberately not configurable. It is a service-side
        # limit, not a behavioural choice: an operator lowering it would change
        # throughput and nothing else, and an operator raising it would get a
        # rejected request with a service-side error message. The number that
        # matters — the chunk size — is already configuration.
        for start in range(0, submitted, _UPSERT_BATCH):
            batch = records[start : start + _UPSERT_BATCH]
            vectors = [_record_payload(record) for record in batch]

            # No `dimension`, no `values`: the service derives both from the
            # index schema (R-002, R-005). Stating either would be Principle VII
            # violated, and stating `values` is additionally a hard 400.
            result = await self._call(
                "upsert",
                self.index.upsert_records,
                namespace=target_namespace,
                records=vectors,
            )
            written += _acknowledged_count(result, len(batch))

        _log.info(
            "upserted vectors",
            extra={"submitted": submitted, "acknowledged": written, "namespace": target_namespace},
        )
        return BatchResult(written=written, submitted=submitted)

    async def search(
        self,
        *,
        query: str,
        top_k: int,
        namespace: str | None = None,
        metadata_filter: dict[str, Any] | None = None,
    ) -> list[SearchHit]:
        """Nearest neighbours, embedding the query with `input_type=query`.

        **`fields=[]` is a deliberate request for no record data, not a
        leftover.** A search returns ids and scores; the text is fetched from
        the registry (`document_units`) by the caller, because the registry is
        the authoritative copy of a unit's content and its heading path
        (data-model.md §4), and the vector store's copy has neither authority nor
        a heading path. Asking for the text here would return a second,
        unauthoritative copy of third-party content into the answer path.

        The empty list is what keeps the response small for the one call that
        runs on every question. It is worth a smoke-test assertion: if the
        service treats an empty `fields` as "omit the argument" it would return
        every field on every record, which is a silent transfer of the whole
        corpus on each query. T027 asserts the response carries no `chunk_text`.
        """
        if top_k <= 0:
            raise ValueError("top_k must be positive")

        target_namespace = namespace or self._namespace

        response = await self._call(
            "search",
            self.index.search,
            namespace=target_namespace,
            top_k=top_k,
            inputs={"text": query},
            fields=[],
            filter=_to_metadata_filter(metadata_filter),
        )

        return self._hits_from(response, namespace=target_namespace)

    def _hits_from(self, response: Any, *, namespace: str) -> list[SearchHit]:
        """Normalise the SDK's response into `SearchHit` objects.

        The response shape, confirmed by introspecting `pinecone==10.0.0`
        (R-002's follow-up), is three levels deep with **no namespace anywhere**
        in it:

            SearchRecordsResponse
              .result  -> SearchResult        (struct fields: ('hits',) only)
                .hits  -> list[Hit]
                  .id / .score / .fields

        Two things follow from that, and both were wrong in a first pass at this
        function:

        **`.result` is a single object, not a list of per-namespace blocks.**
        Iterating it is a `TypeError`, and the SDK's own docstring calls the extra
        `result` step "the usual first stumble". So this walks one level, not a
        list.

        **The namespace is not returned, so the request supplies it.** Every hit
        in a response came from the namespace that was asked for, so the value
        is threaded in from the caller rather than read from the response — which
        is why it is a parameter here and not a `getattr`.

        Hit identifiers are read as `hit.id` and `hit.score`. The wire names are
        `_id` and `_score` and the struct field is `id_`, but `Hit` exposes
        `id`/`score` as properties precisely so callers do not have to know
        that; reaching for `_id` here would be depending on a rename the SDK
        documents as an implementation detail.
        """
        hits: list[SearchHit] = []

        result = getattr(response, "result", None)
        raw_hits = getattr(result, "hits", None) if result is not None else None

        for hit in raw_hits or []:
            score = getattr(hit, "score", None)
            identifier = getattr(hit, "id", None)
            if identifier is None or not isinstance(score, (int, float)):
                # A hit with no id cannot be cited and a hit with no score
                # cannot be ranked. Dropping both is better than passing a None
                # downstream, where it becomes a comparison error far from the
                # cause.
                continue
            metadata = dict(getattr(hit, "fields", None) or {})
            metadata.setdefault("namespace", namespace)
            hits.append(SearchHit(id=str(identifier), score=float(score), metadata=metadata))

        # Sorted defensively. The service is documented to return descending
        # order, but a rerank stage that assumes it and receives otherwise
        # produces a subtly wrong evidence set with nothing to indicate why.
        hits.sort(key=lambda hit: hit.score, reverse=True)
        return hits

    async def delete(self, ids: list[str], *, namespace: str | None = None) -> int:
        """Delete by id.

        This is the call that must work. data-model.md §3.2 makes vector removal
        on transition to `deleted` the entire mechanism behind FR-054 and FR-056,
        and a silent failure here leaves deleted content queryable — which is the
        failure the whole tombstone design exists to prevent.

        Pinecone's `delete` with no ids is a **namespace wipe**, not a no-op.
        So an empty list raises rather than being passed through: a caller that
        computed no ids has a bug, and turning that bug into a total data loss
        would be a catastrophic misreading of it.
        """
        if not ids:
            raise ValueError(
                "delete() requires at least one id; an empty list would wipe the namespace"
            )

        await self._call(
            "delete", self.index.delete, ids=ids, namespace=namespace or self._namespace
        )
        return len(ids)

    async def delete_namespace(self, *, namespace: str | None = None) -> None:
        """Delete an entire namespace. Only for a full re-index."""
        await self._call(
            "delete_namespace",
            self.index.delete_namespace,
            namespace=namespace or self._namespace,
        )
        _log.warning("namespace deleted", extra={"namespace": namespace or self._namespace})

    async def fetch(self, ids: list[str], *, namespace: str | None = None) -> list[VectorRecord]:
        """Fetch records including text, by id."""
        if not ids:
            return []

        response = await self._call(
            "fetch",
            self.index.fetch,
            ids=ids,
            namespace=namespace or self._namespace,
        )

        vectors = getattr(response, "vectors", None)
        if vectors is None and isinstance(response, dict):
            vectors = response.get("vectors", {})

        records: list[VectorRecord] = []
        for identifier, vector in (vectors or {}).items():
            fields = getattr(vector, "metadata", None) or {}
            records.append(
                VectorRecord(
                    id=str(getattr(vector, "id", None) or identifier),
                    # The embedded field is recovered from metadata: on an
                    # integrated index the text is not a separate attribute,
                    # it is what the field_map points at.
                    text=str(fields.get(_TEXT_FIELD, "")),
                    metadata=dict(fields),
                )
            )
        return records


# ============================================================================
# Index provisioning
# ============================================================================


async def ensure_index(
    *,
    create_if_missing: bool = True,
    client: Any | None = None,
) -> IndexInfo:
    """Create the configured index if it does not exist, and report its state.

    Resolved against the installed SDK (R-002). Three findings shaped this
    function, all from introspecting `pinecone==10.0.0` rather than from
    documentation, and the first one is a name that does not exist at all:

    - **`create_for_model` does not exist on the `Pinecone` client** — it exists
      on `Pinecone.indexes`. The two classes expose different surfaces, so a
      negative `hasattr` on one says nothing about the other. This calls
      `pc.indexes.create_for_model`.
    - **`Pinecone.create_index_for_model` is a backwards-compatibility shim.**
      Its own docstring — quoted rather than paraphrased, because the direction
      is the whole point — says: *"Backwards-compatibility shim for
      `Pinecone.indexes.create_for_model`. New code should use
      `pc.indexes.create_for_model()` instead."*
    - **`Pinecone.has_index` is a shim too**, for `indexes.exists`, and this uses
      the latter for the same reason.
    - **`pc.indexes.create(spec=IntegratedSpec(...))` raises** `PineconeTypeError`
      in 10.0.0, naming `create_for_model` as the replacement. The class is still
      importable, so `hasattr` says yes while the code path is unavailable.

    `embed` is passed as a **dict**, which is the form the SDK's own worked
    example uses, rather than as an `IndexEmbed` or `EmbedConfig` object. The
    two spec classes are not interchangeable and the dict is the documented
    spelling for the parameter's declared type (`Mapping[str, Any] | Any`).

    `deletion_protection` is left unset rather than set to `"disabled"`. This is
    a demonstration index whose contents are reproducible from public
    documentation by re-running the crawl, and a protection setting that
    prevents a re-index is a setting that makes the demonstration fail to
    recover from its own most likely mistake.
    """
    settings = get_settings()
    store = PineconeVectorStore(client=client)
    pinecone_client = store.client

    if await asyncio.to_thread(pinecone_client.indexes.exists, store.index_name):
        _log.info("index already exists", extra={"index": store.index_name})
        return await store.describe()

    if not create_if_missing:
        raise VectorServiceUnavailable()

    _log.info(
        "creating index",
        extra={
            "index": store.index_name,
            "cloud": settings.pinecone_cloud,
            "region": settings.pinecone_region,
        },
    )

    await asyncio.to_thread(
        pinecone_client.indexes.create_for_model,
        name=store.index_name,
        cloud=settings.pinecone_cloud,
        region=settings.pinecone_region,
        embed={
            "model": settings.pinecone_embed_model,
            "field_map": {"text": _TEXT_FIELD},
            # The `input_type` asymmetry is the documented correct configuration
            # (R-001). It is written once, here, at creation, and the service
            # stores it on the index's semantic field — which is why `describe()`
            # can read it back and check it, rather than this codebase having to
            # remember what it asked for.
            "read_parameters": {"input_type": "query", "truncate": "END"},
            "write_parameters": {"input_type": "passage", "truncate": "END"},
        },
    )

    info = await store.describe()
    _log.info("index created", extra={"index": info.name, "state": info.state, "ready": info.ready})
    return info


# ============================================================================
# Constants and module access
# ============================================================================

#: The vector-store bound on one upsert call.
_UPSERT_BATCH: int = 100

#: The metadata key the text is stored under, matching `field_map`. Named once
#: and used by the upsert, the fetch, and the search field selection, so the
#: three cannot disagree about where the text lives.
#:
#: It is also the *schema* field name, which is why an upsert sends the text
#: here as a top-level record key rather than inside `metadata`. T027 measured
#: this: `upsert` with `values=[0.0]` returns
#: `Vector dimension 1 does not match the dimension of the index 1024`, while
#: `upsert_records` with this key at the top level succeeds.
_TEXT_FIELD: str = "chunk_text"


def _parameter(parameters: Any, key: str) -> str | None:
    """Read one key out of a `read_parameters`/`write_parameters` mapping.

    The service returns these as a mapping whose exact shape is not pinned by
    the SDK, so this handles the two real possibilities — a dict, or a JSON
    string — and returns `None` for anything else.

    `None` is meaningfully different from a value here. `None` means "the
    service did not tell us", and `input_type_mismatch()` treats it as "nothing
    to check" rather than as a mismatch, because inventing a failure from a
    missing field would make `/health` red for a reason that is not real.
    """
    if isinstance(parameters, dict):
        value = parameters.get(key)
    elif isinstance(parameters, str):
        try:
            import json

            parsed = json.loads(parameters)
        except ValueError:
            return None
        value = parsed.get(key) if isinstance(parsed, dict) else None
    else:
        return None
    return value if isinstance(value, str) else None


def _record_payload(record: VectorRecord) -> dict[str, Any]:
    """One record in the shape `upsert_records` accepts: fields at the top level.

    Three measured facts about this shape, all from T027 running against a live
    index, and each one a hard 400 when got wrong:

    - The id field is `_id`. The SDK documents `id` as accepted-and-dropped, but
      the service's own errors name records by `_id`, so this is the field an
      error can be read back from.
    - The text sits under the schema's `semantic_text` field name, at the top
      level, because that is the field the service embeds from.
    - **Metadata keys are top level too, not nested.** A nested
      `{"metadata": {...}}` is rejected with `Invalid type for field 'metadata'`;
      the service has no nested-object metadata. Scalars, booleans, and lists of
      strings all round-trip — `heading_path` survives as a real list, which
      FR-018 depends on.

    The collision check exists because the metadata is spread last and would
    otherwise win silently. A metadata key of `chunk_text` or `_id` would
    replace the text or the identity of the record, and the service would accept
    the write — so the corruption would surface as a citation pointing at the
    wrong text, with no error anywhere. It is raised rather than dropped: the
    caller is passing metadata it should not be passing, and quietly discarding
    it would leave the pipeline believing the unit was indexed with data it was
    not.
    """
    reserved = {key for key in record.metadata if key in ("_id", "id", _TEXT_FIELD)}
    if reserved:
        raise ValueError(
            f"record {record.id!r} carries metadata key(s) {sorted(reserved)} that collide "
            f"with the record's own fields ('_id', {_TEXT_FIELD!r}); they would be "
            "overwritten on the way to the service"
        )

    return {
        "_id": record.id,
        _TEXT_FIELD: record.text,
        **record.metadata,
    }


def _acknowledged_count(result: Any, attempted: int) -> int:
    """How many records the service says it accepted.

    **Reads `record_count`, the field on the real `UpsertRecordsResponse`.** It
    used to read `upsertedCount`, which belongs to the older `upsert` reply. On
    the `upsert_records` path that name is simply absent, so the function fell
    through to its last line and returned `attempted` — meaning **every write was
    reported as fully successful regardless of what the service actually
    acknowledged**. A short write would have surfaced much later as an
    unanswerable question with no ingestion error to explain it, which is
    precisely the failure `BatchResult.written` exists to prevent.

    `upsertedCount` is still read, for a mapping-shaped response. It is not dead
    weight: a dict is what a caller passing a hand-built stub would get, and
    silently returning `attempted` for it would repeat the bug above.

    An unrecognised response is reported as the attempted count, not as zero. A
    `None` or unexpected shape is the service being uncommunicative, and
    reporting the attempted count is the safer error: a later read will reveal a
    shortfall, whereas a false alarm stops the pipeline for no reason.
    """
    if isinstance(result, dict):
        for name in ("record_count", "upsertedCount"):
            count = result.get(name)
            if isinstance(count, int):
                return count
    record_count = getattr(result, "record_count", None)
    if isinstance(record_count, int):
        return record_count
    if isinstance(result, int):
        return result
    return attempted


_store: PineconeVectorStore | None = None


def get_vector_store() -> PineconeVectorStore:
    """The process-wide store. Cached so the connection is reused."""
    global _store
    if _store is None:
        _store = PineconeVectorStore()
    return _store


def reset_vector_store() -> None:
    """Drop the cached store. Test teardown."""
    global _store
    _store = None


__all__ = [
    "BatchResult",
    "IndexInfo",
    "PineconeVectorStore",
    "SearchHit",
    "VectorRecord",
    "VectorStore",
    "ensure_index",
    "get_vector_store",
    "reset_vector_store",
]
