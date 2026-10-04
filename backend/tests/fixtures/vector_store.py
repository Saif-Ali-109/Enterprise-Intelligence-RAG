"""An in-memory `VectorStore` that records what it was asked to do.

The pipeline's two vector-writing decisions — which units to write, and which
to remove — are only observable through the store's calls. A fake that keeps
its records and counts its calls turns "repeated crawls never accumulate
duplicates" from a claim about production into an assertion about a list.

It implements the protocol structurally (no inheritance), because a fake that
inherits from the real store can pass by inheriting behaviour the production
store does not have. `write_calls` and `delete_calls` are lists of *calls*,
not totals, so a test can assert both the count and the arguments.

Deliberately not here: embed models, similarity, namespaces, and cost. This
fake answers "what was written", which is the only question the ingestion tests
ask of a vector store.
"""

from __future__ import annotations

from typing import Any

from app.retrieval.vector_store import BatchResult, IndexInfo, VectorRecord


class RecordingVectorStore:
    """The `VectorStore` protocol, in memory, with a record of every call."""

    def __init__(self) -> None:
        self.records: dict[str, VectorRecord] = {}
        self.write_calls: list[list[str]] = []
        self.delete_calls: list[list[str]] = []
        #: Ids dropped by `delete`. A deletion that the store did not
        #: acknowledge would silently leave content retrievable, so the fake
        #: counts it the way the real adapter does.
        self.deleted: list[str] = []

    async def describe(self) -> IndexInfo:
        raise NotImplementedError("the ingestion path never describes an index")

    async def upsert(
        self, records: list[VectorRecord], *, namespace: str | None = None
    ) -> BatchResult:
        if namespace is not None:
            raise AssertionError("ingestion must not pass a namespace; it is reserved by the store")
        self.write_calls.append([record.id for record in records])
        for record in records:
            # An upsert overwrites, exactly as the service does: this is what
            # makes supersession under an immutable vector id work.
            self.records[record.id] = record
        return BatchResult(written=len(records), submitted=len(records))

    async def search(
        self,
        *,
        query: str,
        top_k: int,
        namespace: str | None = None,
        metadata_filter: dict[str, Any] | None = None,
    ) -> list[Any]:
        raise NotImplementedError("the ingestion path never searches")

    async def delete(self, ids: list[str], *, namespace: str | None = None) -> int:
        self.delete_calls.append(list(ids))
        removed = 0
        for identifier in ids:
            if self.records.pop(identifier, None) is not None:
                removed += 1
            self.deleted.append(identifier)
        return removed

    async def delete_namespace(self, *, namespace: str | None = None) -> None:
        self.records.clear()

    async def fetch(self, ids: list[str], *, namespace: str | None = None) -> list[VectorRecord]:
        return [self.records[i] for i in ids if i in self.records]

    # -- test conveniences ------------------------------------------------

    @property
    def live_ids(self) -> list[str]:
        return sorted(self.records)

    @property
    def writes(self) -> int:
        return sum(len(call) for call in self.write_calls)


__all__ = ["RecordingVectorStore"]
