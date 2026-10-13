"""A vendor quota must not reach a client as a 500 (FR-042, FR-047).

Measured 2026-10-13: with the organisation's rerank quota exhausted, every
question returned

    500 INTERNAL_ERROR  "An unexpected error occurred."

while the log carried `[429 RESOURCE_EXHAUSTED] Request failed. You've reached
the rerank request limit (500) for the current month`. A client could not tell a
usage limit from a defect, and the only useful information was in a log line an
operator had to go looking for.

The reranker now translates the vendor's failures into this system's vocabulary:
a rate limit becomes `ProviderRateLimited` — which the evaluation runner already
retries with backoff — and everything else becomes `ProviderError`. Neither
carries the vendor's message, because that text contains the account identifier
(FR-042).
"""

from __future__ import annotations

from typing import Any

import pytest

pytestmark = pytest.mark.unit


class _Inference:
    """A Pinecone inference stub that fails the way the vendor fails."""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc
        self.calls = 0

    def rerank(self, **_kwargs: Any) -> Any:
        self.calls += 1
        raise self._exc


class _RateLimitError(Exception):
    """Named like the vendor's, because the translation matches on the name."""


class _BadRequest(Exception):
    pass


def _reranker(inference: _Inference) -> Any:
    from app.retrieval.reranker import PineconeReranker

    reranker = PineconeReranker.__new__(PineconeReranker)
    reranker._client = object()  # noqa: SLF001 - no vendor client is constructed
    reranker._model = "bge-reranker-v2-m3"
    object.__setattr__(reranker, "_inference_override", inference)
    return reranker


def _candidates() -> list[Any]:
    from app.retrieval.reranker import RerankCandidate

    return [RerankCandidate(id="v1", text="A page about filters.", retrieval_score=0.8)]


async def _run(inference: _Inference) -> None:
    from app.retrieval.reranker import PineconeReranker

    # `rerank` reads the client through the `_inference` property, so the stub is
    # installed by patching that property rather than a private attribute.
    original = PineconeReranker._inference
    try:
        PineconeReranker._inference = property(lambda self: inference)  # type: ignore[assignment]
        await PineconeReranker.rerank(
            _reranker(inference), query="How do I save a filter?", candidates=_candidates(), top_n=6
        )
    finally:
        PineconeReranker._inference = original  # type: ignore[assignment]


async def test_a_rerank_rate_limit_becomes_a_retryable_code() -> None:
    """`ProviderRateLimited` is the code the evaluation runner retries.

    A per-minute limit is worth retrying; an untyped 500 is worth nothing.
    """
    from app.core.errors import ErrorCode, ProviderRateLimited

    with pytest.raises(ProviderRateLimited) as caught:
        await _run(_Inference(_RateLimitError("429 RESOURCE_EXHAUSTED for account org-abc123")))

    assert caught.value.code is ErrorCode.PROVIDER_RATE_LIMITED


async def test_any_other_vendor_failure_becomes_a_provider_error() -> None:
    from app.core.errors import ErrorCode, ProviderError

    with pytest.raises(ProviderError) as caught:
        await _run(_Inference(_BadRequest("400 INVALID_ARGUMENT")))

    assert caught.value.code is ErrorCode.PROVIDER_ERROR


async def test_the_vendors_message_never_reaches_the_error() -> None:
    """The vendor's text names the account; this system's errors must not (FR-042)."""
    from app.core.errors import ProviderError, ProviderRateLimited

    secret = "org-abc123-team-xyz789"

    with pytest.raises(ProviderRateLimited) as rate_limited:
        await _run(_Inference(_RateLimitError(f"429 RESOURCE_EXHAUSTED for {secret}")))
    assert secret not in str(rate_limited.value)

    with pytest.raises(ProviderError) as failed:
        await _run(_Inference(_BadRequest(f"rejected key for {secret}")))
    assert secret not in str(failed.value)


async def test_a_successful_call_still_returns_hits() -> None:
    """The translation wraps the call; it does not replace it."""
    from app.retrieval.reranker import PineconeReranker

    class _OK(_Inference):
        def rerank(self, **kwargs: Any) -> Any:  # type: ignore[override]
            self.calls += 1
            return {"data": [{"index": 0, "score": 0.91}]}

    original = PineconeReranker._inference
    ok = _OK(Exception("unused"))
    try:
        PineconeReranker._inference = property(lambda self: ok)  # type: ignore[assignment]
        hits = await PineconeReranker.rerank(
            _reranker(ok), query="q", candidates=_candidates(), top_n=6
        )
    finally:
        PineconeReranker._inference = original  # type: ignore[assignment]

    assert len(hits) == 1
    assert hits[0].rerank_score == pytest.approx(0.91)
