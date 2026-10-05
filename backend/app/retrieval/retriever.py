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

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.models import Document, DocumentUnit
from app.retrieval.query_analyzer import QueryAnalysis
from app.retrieval.vector_store import SearchHit, VectorStore

FilterReason = Literal[
    "confident",
    "low_confidence",
    "disabled",
    "no_analysis",
    "ambiguous",
]


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
    if analysis.ambiguity_note is not None:
        # The question names more than one product, so the analyser's single
        # answer is one of several and filtering on it would silently drop the
        # rest. Suppressed for a reason the response can report (edge case 13).
        return FilterPlan(filter=None, suppressed=True, reason="ambiguous")

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


def should_widen(plan: FilterPlan, hits: Sequence[Any]) -> bool:
    """Whether an empty *filtered* pool should be re-run with no filter.

    The narrowest reading of FR-010 is "widen when confidence is low", and this
    is the case it misses: confidence was 0.99 and the filter was still wrong,
    because a category vocabulary that has drifted from the corpus cannot match
    anything. Measured on the first live retrieval run — `{"product": "jira",
    "category": "api"}` against a corpus of `rest-api` returned zero candidates
    and produced a refusal for a page that answered the question.

    Two properties keep this from becoming "search twice always":

    * It fires only when a filter was actually applied. An already-wide search
      that found nothing is a genuine absence of evidence and must stay one —
      widening it again would be a second identical query pretending to be
      diligence.
    * It costs one extra search, once per rewrite query, and the response
      reports the widened scope, so the widening is visible rather than a
      silently better-looking result.
    """
    return plan.filter is not None and not hits


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


async def retrieve(
    query: str,
    *,
    session: AsyncSession,
    store: VectorStore,
    analysis: QueryAnalysis | None = None,
    top_k: int | None = None,
    namespace: str | None = None,
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
    # The namespace is configuration, not a constant in this file: writing and
    # reading must agree, and a hard-coded default here reads a *different*
    # namespace than the store writes whenever PINECONE_NAMESPACE is set. The
    # symptom of that is a clean registry with an empty candidate pool, which
    # looks exactly like "nothing indexed yet".
    ns = namespace if namespace is not None else settings.pinecone_namespace

    plan = build_metadata_filter(analysis)
    hits: list[SearchHit] = await store.search(
        query=query,
        top_k=pool,
        namespace=ns,
        metadata_filter=plan.filter,
    )
    if not hits:
        return []

    ids = [h.id for h in hits]
    records = await store.fetch(ids, namespace=ns)
    records_by_id = {r.id: r for r in records}

    # Provenance is joined from the registry, not read from the index. The
    # registry is the system of record for "which publisher page is this", which
    # is what FR-004 requires a citation to resolve through, and the join costs
    # one query. Measured against the live index on 2026-10-04, on
    # `pinecone==10.0.0`: `Index.search` returns hits whose `metadata` is `None`
    # for every `fields` value tried — `[]`, `["*"]`, a named list, and the
    # parameter omitted entirely — and `Index.search` has no `include_metadata`
    # parameter to ask with. `Index.fetch` does return metadata, but fetch
    # answered empty for freshly written ids long after search had them, so
    # neither index-side source is dependable for provenance.
    rows = (
        await session.execute(
            select(DocumentUnit, Document)
            .join(Document, DocumentUnit.document_id == Document.id)
            .where(DocumentUnit.vector_id.in_(ids))
        )
    ).all()
    units_by_vector_id = {unit.vector_id: (unit, document) for unit, document in rows}

    candidates: list[Candidate] = []
    for hit in hits:
        found = units_by_vector_id.get(hit.id)
        if found is None:
            # A vector the registry no longer trusts: deleted, or indexed by a
            # code path that never recorded it. Dropping it from the pool is the
            # only safe answer — extracting evidence from a record the registry
            # cannot answer for would put an unverifiable claim in the
            # answer stage.
            continue
        unit, document = found
        record = records_by_id.get(hit.id)
        source_url = document.url or document.canonical_url or ""
        if not source_url:
            # Uncited evidence is not evidence: a candidate the registry cannot
            # name a page for cannot become a validated citation.
            continue
        candidates.append(
            Candidate(
                id=hit.id,
                unit_id=unit.id,
                document_id=unit.document_id,
                text=record.text if record is not None else "",
                retrieval_score=hit.score,
                product=document.product,
                category=document.category,
                page_type=document.page_type,
                title=document.title,
                source_url=source_url,
                heading_path=list(unit.heading_path) if unit.heading_path is not None else [],
                token_count=unit.token_count,
                ordinal=unit.ordinal,
                metadata={
                    "unit_id": str(unit.id),
                    "document_id": str(unit.document_id),
                    "source_id": str(document.source_id),
                    "source_url": source_url,
                    "title": document.title,
                    "product": document.product,
                    "category": document.category,
                    "page_type": document.page_type,
                    "language": document.language,
                },
            )
        )
    return candidates


__all__ = ["Candidate", "FilterPlan", "build_metadata_filter", "retrieve", "should_widen"]
