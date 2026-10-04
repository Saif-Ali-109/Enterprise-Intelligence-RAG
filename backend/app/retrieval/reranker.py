"""The `Reranker` interface, and the Pinecone-hosted adapter that implements it.

T031. R-004, FR-019, FR-020, FR-021. Stage 2 of the retrieval budget:
12 candidates become 6, and from those 6 the evidence selector takes 3 to 6.

**The reranker is a separate vendor call, not a fused `search(rerank=…)`.** R-004
chose this, and the reasoning is worth restating because it looks like an
optimisation to reverse: a fused call would be one round trip instead of two,
and it would fuse two independently specified stages into one API call. FR-019
requires the reranking stage to be swappable *without altering the pipeline* — a
fused call cannot be, because replacing the reranker means replacing the
retrieval call. The extra round trip buys the interface, and the interface is
the requirement.

**The reranker needs text, so the text is hydrated from the registry first.**
This is the load-bearing structural decision in the module. A reranker ranks
*documents*; it cannot rank ids. The search response deliberately returns no
record data (`fields=[]` in `vector_store.search`), because the registry is the
authoritative copy of a unit's content and its heading path. So the retriever
hydrates its candidates from `document_units` **before** reranking, and this
module's input type makes that ordering a type error rather than a runtime
discovery: a `RerankCandidate` with empty `text` is refused below, because
reranking an empty string returns a confident score for nothing.

**Scores from the two stages are kept separate and never combined.** Rerank
scores and retrieval similarity come from different models on different scales.
`contracts/README.md` is explicit that the API never presents them on a shared
scale or averages them, and FR-021 scores them as independent signals. A single
blended score is the most natural thing to write and the one thing that would
make both numbers meaningless.

**Candidates are mapped back by position, using `RankedDocument.index`.** The
SDK documents `index` as "the position this document held in the *documents*
argument", which is exactly the mapping needed. An earlier draft prefixed each
document with an `[id]=…` marker to recover the id from `return_documents=True`;
that was wrong on two counts, both found by introspecting the installed SDK
rather than by reading its docs. With `return_documents=False` the document is
never returned, so the marker came back as `None`. And prefixing every document
with an id string hands the reranker tokens that are not in the query, which
degrades exactly the score this stage exists to produce. `index` needs no marker.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from app.core.config import get_settings
from app.core.errors import ProviderError
from app.core.logging import get_logger

_log = get_logger("retrieval.reranker")


# ============================================================================
# Value objects
# ============================================================================


@dataclass(frozen=True, slots=True)
class RerankCandidate:
    """A candidate to be reranked: the unit's text plus where it came from.

    A distinct type from `SearchHit`, not a subclass and not a reuse, and the
    distinction is the point. `SearchHit` is what a search returns — an id, a
    similarity, and metadata — and it carries no text by design. Passing
    `SearchHit` to a reranker is a type error rather than a silent rerank of
    nothing, which is the outcome a shared type would have produced.
    """

    id: str
    text: str
    retrieval_score: float
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RerankedHit:
    """A reranked candidate, carrying BOTH scores.

    `retrieval_score` is the similarity the embed model produced and
    `rerank_score` is the relevance the reranker produced. They live side by
    side rather than being blended. The evidence selector reads them
    independently (FR-021), and an operator inspecting an answer needs to see
    that a candidate was retrieved strongly and reranked weakly — which is a
    meaningful signal about the query, not a defect to smooth over.
    """

    id: str
    text: str
    retrieval_score: float
    rerank_score: float
    rank: int
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_candidate(
        cls, candidate: RerankCandidate, *, rank: int, rerank_score: float
    ) -> RerankedHit:
        return cls(
            id=candidate.id,
            text=candidate.text,
            retrieval_score=candidate.retrieval_score,
            rerank_score=rerank_score,
            rank=rank,
            metadata=dict(candidate.metadata),
        )


# ============================================================================
# The interface
# ============================================================================


@runtime_checkable
class Reranker(Protocol):
    """Reorders candidates by relevance to a query.

    A `Protocol` for the same reason `VectorStore` is one: a test must be able
    to supply a deterministic ranking without a vendor account, and swapping
    `bge-reranker-v2-m3` for another hosted model must not touch the retriever.
    """

    async def rerank(
        self,
        *,
        query: str,
        candidates: list[RerankCandidate],
        top_n: int,
    ) -> list[RerankedHit]:
        """Reorder and truncate to `top_n`. Empty input returns empty output."""
        ...


# ============================================================================
# Pinecone-hosted adapter
# ============================================================================


class PineconeReranker:
    """`Reranker` over `pc.inference.rerank`.

    The signature, confirmed by introspecting `pinecone==10.0.0`:

        rerank(model, query, documents, rank_fields=['text'],
               return_documents=True, top_n=None, parameters=None) -> RerankResult

    where `RerankResult` is `(model: str, data: list[RankedDocument],
    usage: RerankUsage)` and `RankedDocument` is `(index: int, score: float,
    document: dict | None)`. `document` is `None` when `return_documents=False`,
    which is why the id travels positionally via `index` and not in the text.

    `top_n` is bounded by the candidate count before the call rather than left
    to the service to clamp. A value derived from configuration alone would
    make the candidate count the effective limit — a silent cap where the code
    says otherwise, which is the failure mode this class exists to avoid.
    """

    def __init__(self, *, model: str | None = None, client: Any | None = None) -> None:
        settings = get_settings()
        self._model = model or settings.pinecone_rerank_model
        self._client = client

    @property
    def model(self) -> str:
        """The rerank model in use. Reported by `/config`."""
        return self._model

    @property
    def _inference(self) -> Any:
        """The inference API handle, built on first use.

        Lazy for the same reason the vector store's client is: a test that
        imports the retriever with a dummy key and a fake should not need a
        real client to exist. `Settings` already refuses to start without a key,
        so an eager client would make the module unimportable in exactly the
        tests that need to import it.
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
                raise ProviderError(
                    "The reranker client could not be constructed. Check PINECONE_API_KEY."
                ) from exc
        return self._client.inference

    async def rerank(
        self,
        *,
        query: str,
        candidates: list[RerankCandidate],
        top_n: int,
    ) -> list[RerankedHit]:
        """Reorder `candidates` by relevance to `query`, truncated to `top_n`."""
        if not candidates:
            return []
        if top_n <= 0:
            raise ValueError("top_n must be positive")

        usable = [candidate for candidate in candidates if candidate.text.strip()]
        if not usable:
            # Every candidate failed to hydrate. Reranking empty strings would
            # return six confident scores for six documents that do not exist,
            # and the evidence selector would have no way to tell that from a
            # real ranking. An empty result is the honest answer, and it routes
            # to a refusal rather than to a fabricated citation.
            _log.error(
                "no rerank candidate had text; the registry join returned nothing",
                extra={"candidates": len(candidates)},
            )
            return []

        if len(usable) < len(candidates):
            _log.warning(
                "dropped candidates with no text before reranking",
                extra={"dropped": len(candidates) - len(usable), "usable": len(usable)},
            )

        effective_top_n = min(top_n, len(usable))

        # Clean text, nothing prepended. See the module docstring for what
        # happens to the ranking when an id marker is mixed in.
        documents = [candidate.text for candidate in usable]

        result = await asyncio.to_thread(
            self._inference.rerank,
            model=self._model,
            query=query,
            documents=documents,
            rank_fields=["text"],
            return_documents=False,
            top_n=effective_top_n,
            # Measured 2026-10-04 against the live service: with a 1000-token unit
            # and a one-line question the service answers
            #
            #   400 INVALID_ARGUMENT Request contains a query+document pair with
            #   3157 tokens, which exceeds the maximum token limit of 1024 for
            #   each query+document pair. Consider setting "parameters.truncate"
            #   to "END" to truncate long query+document pairs.
            #
            # The chunker's upper bound is 1000 tokens and the query adds to it,
            # so *every* question about a maximum-size unit would fail the whole
            # request — a refusal caused by a document's length, not its
            # content. Truncating at the end keeps the beginning of the unit,
            # which is where a heading and its first paragraph live.
            parameters={"truncate": "END"},
        )

        return self._hits_from(result, usable, effective_top_n)

    def _hits_from(
        self,
        result: Any,
        candidates: list[RerankCandidate],
        top_n: int,
    ) -> list[RerankedHit]:
        """Map the service's `(index, score)` pairs back onto candidates.

        Falls back to the original retrieval order if the response cannot be
        interpreted, because an unreranked candidate list is a degraded ranking
        — visibly so, since every `rerank_score` then equals its
        `retrieval_score`. Inventing scores to fill the gap would be worse, and
        FR-021's two independent signals would collapse into one.
        """
        data = getattr(result, "data", None)
        if data is None and isinstance(result, dict):
            data = result.get("data")

        if not isinstance(data, list):
            _log.warning(
                "rerank response unrecognised; falling back to retrieval order",
                extra={"top_n": top_n},
            )
            return _as_reranked(candidates[:top_n])

        served_by = getattr(result, "model", None)
        if served_by and served_by != self._model:
            # The SDK documents that the model which served a request is not
            # always the one asked for. A substituted reranker is not a
            # failure, but it is a fact about the score the user is shown, so
            # it is recorded rather than assumed away.
            _log.warning(
                "rerank served by a different model than requested",
                extra={"requested": self._model, "served": served_by},
            )

        scored: list[tuple[float, int, RerankCandidate]] = []
        for entry in data:
            index = entry.get("index") if isinstance(entry, dict) else getattr(entry, "index", None)
            score = entry.get("score") if isinstance(entry, dict) else getattr(entry, "score", None)

            if not isinstance(index, int) or not isinstance(score, (int, float)):
                continue
            # `index` is relative to the `documents` argument, which was
            # `usable` — not to `candidates`. A bounds check against the wrong
            # list is how a score ends up on the wrong citation, so the bound
            # and the lookup use the same list.
            if not 0 <= index < len(candidates):
                _log.error(
                    "rerank returned an out-of-range index",
                    extra={"index": index, "candidates": len(candidates)},
                )
                continue

            scored.append((_clamp_score(float(score)), index, candidates[index]))

        if not scored:
            _log.warning("rerank returned no usable rows; falling back to retrieval order")
            return _as_reranked(candidates[:top_n])

        # Sorted by descending score even though the SDK documents `data` as
        # already ordered that way. `rank` is assigned by this method, so
        # trusting the order means a response that arrives out of order produces
        # ranks that are simply wrong — and `rank` is what the evidence selector
        # and the response's citation ordering read. A defensive sort costs
        # nothing and removes a dependency on a documented-but-unenforced
        # guarantee.
        #
        # Ties break on the candidate's own position, so a reranker that returns
        # equal scores produces a deterministic order rather than one that
        # depends on the service's internal sort being stable.
        scored.sort(key=lambda row: (-row[0], row[1]))

        return [
            RerankedHit.from_candidate(candidate, rank=rank, rerank_score=score)
            for rank, (score, _index, candidate) in enumerate(scored, start=1)
        ]


# ============================================================================
# Helpers and module access
# ============================================================================


def _as_reranked(candidates: list[RerankCandidate]) -> list[RerankedHit]:
    """Wrap candidates as reranked hits carrying their retrieval score.

    Used only on the fallback paths. `rerank_score` equals `retrieval_score`,
    which makes the degradation visible to the evidence selector and to an
    inspecting user rather than presenting a fabricated second opinion.
    """
    return [
        RerankedHit.from_candidate(candidate, rank=rank, rerank_score=candidate.retrieval_score)
        for rank, candidate in enumerate(candidates, start=1)
    ]


def _clamp_score(score: float) -> float:
    """Bound a score to [0, 1], logging when the bound actually bites.

    The service documents [0, 1] for this model and the contract constrains
    scores to that range. Clamping hides a miscalibration, so it is logged as
    well as applied — the log line is the only place the original value exists.
    """
    if 0.0 <= score <= 1.0:
        return score
    bounded = max(0.0, min(1.0, score))
    _log.warning(
        "rerank score outside [0,1]; clamped", extra={"score": score, "clamped_to": bounded}
    )
    return bounded


_reranker: PineconeReranker | None = None


def get_reranker() -> PineconeReranker:
    """The process-wide reranker. Cached so the client is reused."""
    global _reranker
    if _reranker is None:
        _reranker = PineconeReranker()
    return _reranker


def reset_reranker() -> None:
    """Drop the cached reranker. Test teardown."""
    global _reranker
    _reranker = None


__all__ = [
    "PineconeReranker",
    "RerankCandidate",
    "RerankedHit",
    "Reranker",
    "get_reranker",
    "reset_reranker",
]
