"""Dense retrieval and filter construction (T081/T082, FR-010/FR-014/FR-020).

Principle VIII: the size of the candidate pool is configuration, not a constant
in this file. A retriever that hard-codes "12" makes a tuning decision without
the operator, and a test that patches out "12" would be forced to patch out its
own meaning.

The filter-construction rules live in `build_metadata_filter` and exist for the
case where the truthful answer is *no filter*: the user's product is unknown and
searching a subset would hide evidence from a stronger (but wrong) guess.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.models import DocumentUnit
from app.retrieval.query_analyzer import QueryAnalysis
from app.retrieval.vector_store import SearchHit, VectorStore

FilterReason = Literal["confident", "low_confidence", "disabled", "no_analysis"]


@dataclass(frozen=True, slots=True)
class FilterPlan:
    """The filter a retrieval runs with, and why.

    `suppressed` is True precisely when `filter` is None and the search is
    meant to be broad. The reason string is part of the explanation an operator
    sees when debugging a wider-than-expected candidate set.
    """

    filter: dict[str, Any] | None
    suppressed: bool
    reason: FilterReason


def build_metadata_filter(
    analysis: QueryAnalysis | None,
    *,
    filters_enabled: bool | None = None,
) -> FilterPlan:
    """Narrow only on what the analysis is actually confident about.

    Rule for each filterable field: the field's value is known AND its
    confidence clears the configured threshold. A field that fails either test
    is omitted rather than replaced with a best guess. If no field clears, the
    plan is "suppress everything": a wide search with a visible reason beats a
    narrow search against a wrong one (FR-010).
    """
    settings = get_settings()
    if filters_enabled is None:
        filters_enabled = settings.filters_enabled
    if not filters_enabled:
        return FilterPlan(filter=None, suppressed=True, reason="disabled")
    if analysis is None:
        return FilterPlan(filter=None, suppressed=True, reason="no_analysis")

    threshold = settings.classification_confidence_threshold
    confidences = analysis.classification_confidence

    terms: dict[str, Any] = {}
    if analysis.detected_product is not None and confidences.get("product", 0.0) >= threshold:
        terms["product"] = analysis.detected_product
    if analysis.detected_category is not None and confidences.get("category", 0.0) >= threshold:
        terms["category"] = analysis.detected_category

    if terms:
        return FilterPlan(filter=terms, suppressed=False, reason="confident")
    return FilterPlan(filter=None, suppressed=True, reason="low_confidence")


@dataclass(frozen=True, slots=True)
class Candidate:
    """One retrieved unit, hydrated for the rerank/evidence stage.

    `retrieval_score` is the embed model's similarity for this query — per the
    `VectorStore` contract it is a similarity in [0, 1], never averaged with
    rerank scores. `text` comes from the store record (the embedded text is the
    authoritative copy); provenance and structure come from the registry and
    travel alongside, so citations assemble from one object rather than three
    joins at every consumer.
    """

    id: str  # vector_id, the stable unit identifier across crawls (R-005)
    unit_id: UUID  # registry row id; what the Citation schema names unit_id
    document_id: UUID
    text: str
    retrieval_score: float
    product: str | None
    category: str | None
    page_type: str | None
    title: str | None
    source_url: str
    heading_path: list[str] = field(default_factory=list)
    token_count: int = 0
    ordinal: int = 0
    # The store-side metadata record, passed through so the reranker can rank
    # (its input type requires text AND metadata instead of a shape that has to
    # be re-derived at the rerank step).
    metadata: dict[str, Any] = field(default_factory=dict)


_DEFAULT_NAMESPACE_CANDIDATE = "atlassian-public"


async def retrieve(
    query: str,
    *,
    session: AsyncSession,
    store: VectorStore,
    analysis: QueryAnalysis | None = None,
    top_k: int | None = None,
    namespace: str = _DEFAULT_NAMESPACE_CANDIDATE,
) -> list[Candidate]:
    """Dense retrieval for one query, through the `VectorStore` protocol only.

    One search, one hydration round trip, one registry join: the hits come back
    with search metadata, the registry confirms each hit's `document_id` and
    heading path (i.e. the row is an indexed *current* unit — a tombstoned or
    orphaned vector id is reported as not-found and dropped from the pool), and
    the embedding fetch supplies the authoritative text for reranking.

    The filter is a `FilterPlan`, not an ad-hoc dict, so a suppressed plan reads
    as an explicit decision rather than an accidentally empty filter —
    `test_retrieval_filters` pins the rule.
    """
    settings = get_settings()
    pool = top_k if top_k is not None else settings.retrieval_candidate_pool

    plan = build_metadata_filter(analysis)
    hits: list[SearchHit] = await store.search(
        query=query,
        top_k=pool,
        namespace=namespace,
        metadata_filter=plan.filter,
    )
    if not hits:
        return []

    ids = [h.id for h in hits]
    records = await store.fetch(ids, namespace=namespace)
    text_by_id = {r.id: r.text for r in records}

    result = await session.execute(select(DocumentUnit).where(DocumentUnit.vector_id.in_(ids)))
    units_by_vector_id = {row.vector_id: row for row in result.scalars()}

    candidates: list[Candidate] = []
    for hit in hits:
        unit = units_by_vector_id.get(hit.id)
        text = text_by_id.get(hit.id, "")
        metadata = hit.metadata or {}
        if unit is None:
            # A vector the registry no longer trusts: deleted, or indexed by a
            # code path that never recorded it. Dropping it from the pool is the
            # only safe answer — extracting evidence from a record the registry
            # cannot answer for would put an unverifiable claim in the
            # answer stage.
            continue
        source_url = str(metadata.get("source_url") or "")
        if not source_url:
            continue
        candidates.append(
            Candidate(
                id=hit.id,
                unit_id=unit.id,
                document_id=unit.document_id,
                text=text,
                retrieval_score=hit.score,
                product=metadata.get("product") or None,
                category=metadata.get("category") or None,
                page_type=metadata.get("page_type") or None,
                title=metadata.get("title") or None,
                source_url=source_url,
                heading_path=list(unit.heading_path) if unit.heading_path is not None else [],
                token_count=unit.token_count,
                ordinal=unit.ordinal,
                metadata={
                    **metadata,
                    # `unit_id` is the registry row — DocumentUnit.id — that a Citation
                    # schema field expects, but retrieval metadata joins on vector_id.
                    # Riding it through candidate metadata means every downstream
                    # step can answer "which row of ours does this vector refer to"
                    # without a second registry round trip.
                    "unit_id": str(unit.id),
                },
            )
        )
    return candidates


__all__ = ["Candidate", "FilterPlan", "build_metadata_filter", "retrieve"]
