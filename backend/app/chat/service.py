"""The chat orchestration service (T088 / US1).

Six stages, one transaction boundary around the user's request: analyse,
retrieve, rerank, select, generate, verify — then the deterministic wrapping.
The loop from generate back to *attempt again* exists once and is capped at two
by `settings.generation_max_attempts`; the verifier can neither forgive a
grounding failure on the second attempt nor ask for a third.

Every writer of the `AskResponse` payload reads it back through this function —
there is no other place that can assemble one, which is what keeps the HTTP
route, the evaluation runner, and the streaming route in agreement about what
"answered" and "refused" mean. The `trace` is attached whenever the caller
asks for it on the request; no reasoning is attached ever.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.chat.audit import record_query
from app.core.config import get_settings
from app.core.errors import AppError, ProviderError
from app.core.logging import get_request_id, new_request_id
from app.generation.generator import generate_answer
from app.generation.provider import LLMProvider
from app.generation.verifier import verify_answer
from app.retrieval.citations import (
    EvidenceSource,
    citation_granularity,
    resolve_answer_markers,
    validate_citations,
)
from app.retrieval.evidence import build_leads, gate_evidence, select_evidence
from app.retrieval.query_analyzer import QueryAnalysis, analyze_question
from app.retrieval.query_rewriter import rewrite_queries
from app.retrieval.reranker import RerankCandidate, RerankedHit, Reranker
from app.retrieval.retriever import build_metadata_filter, retrieve, should_widen
from app.retrieval.vector_store import VectorStore

#: How many weakly related sources a refusal may offer (FR-008). Three, because
#: a list of ten is a search result page, not a hint.
LEAD_LIMIT = 3


def searched_products(analysis: QueryAnalysis) -> list[str | None]:
    """The products the search actually covered.

    For an ambiguous question that is every product it named — the response then
    says which domains were searched, which is what makes the ambiguity note
    actionable rather than decorative. For an ordinary question it is the one
    product the analysis found, or `null` when it found none.
    """
    if analysis.ambiguity_note is not None and analysis.named_products:
        return list(analysis.named_products)
    return [analysis.detected_product]


@dataclass(slots=True)
class ChatResult:
    payload: dict[str, Any]
    stages_ms: dict[str, int] = field(default_factory=dict)


def _refused(
    *,
    request_id: str,
    question: str,
    reason: str,
    searched: dict[str, Any],
    leads: list[dict[str, Any]],
    total_ms: int,
    trace: dict[str, Any] | None,
    query_analysis: dict[str, Any] | None,
    stages_ms: dict[str, int],
) -> ChatResult:
    payload: dict[str, Any] = {
        "request_id": request_id,
        "question": question,
        "outcome": "refused",
        "answer": None,
        "refusal_reason": reason,
        "searched": searched,
        "leads": leads,
        "citations": [],
        "timing": {"total_ms": total_ms, "stages_ms": stages_ms},
    }
    if query_analysis is not None:
        payload["query_analysis"] = query_analysis
    if trace is not None:
        payload["trace"] = trace
    return ChatResult(payload=payload, stages_ms=stages_ms)


async def run_chat(
    question: str,
    *,
    session: AsyncSession,
    store: VectorStore,
    provider: LLMProvider,
    reranker: Reranker | None = None,
    inspect: bool = False,
    started_unix: float | None = None,
) -> ChatResult:
    """The one place an `AskResponse` is assembled, audited on every path (FR-048).

    The wrapper exists so that *every* exit is recorded: the answer, the
    refusal, and the error. The failure path is the one most worth recording and
    the one a pipeline is most likely to skip, because it leaves by exception
    rather than by `return` — and an outage with no rows is an outage nobody can
    investigate afterwards.
    """
    # The request's own id, not a fresh one. FR-047: the value a user quotes in
    # a bug report is the same value that joins the report to the audit record
    # and to the log line — so the error envelope, the `query_logs` row and the
    # log records must all carry it. Minting a second id here produced exactly
    # the split that made a recorded outage untraceable by its own request id,
    # which `test_vector_outage.py` found by looking the id up and not finding it.
    request_id = get_request_id() or new_request_id()
    started = time.perf_counter() if started_unix is None else started_unix
    try:
        result = await _answer(
            question,
            session=session,
            store=store,
            provider=provider,
            reranker=reranker,
            inspect=inspect,
            started_unix=started_unix,
            request_id=request_id,
        )
    except AppError as exc:
        await record_query(
            session,
            request_id=request_id,
            question=question,
            outcome="error",
            error_code=exc.code.value,
            total_latency_ms=int((time.perf_counter() - started) * 1000),
        )
        raise

    payload = result.payload
    analysis_payload = payload.get("query_analysis") or {}
    searched = payload.get("searched") or {}
    await record_query(
        session,
        request_id=request_id,
        question=question,
        outcome=payload["outcome"],
        answer=payload.get("answer"),
        refusal_reason=payload.get("refusal_reason"),
        detected_product=analysis_payload.get("detected_product"),
        detected_category=analysis_payload.get("detected_category"),
        intent=analysis_payload.get("intent"),
        classification_confidence=analysis_payload.get("confidence"),
        rewrite_queries=analysis_payload.get("rewrite_queries"),
        applied_filters=searched.get("applied_filters"),
        candidates_retrieved=searched.get("candidates_retrieved", 0),
        candidates_reranked=searched.get("candidates_reranked", 0),
        evidence_selected=searched.get("evidence_selected", 0),
        stage_latency_ms=result.stages_ms,
        total_latency_ms=payload["timing"]["total_ms"],
        provider="groq",
        model=get_settings().groq_model,
        rerank_model=get_settings().pinecone_rerank_model,
        pipeline_trace=payload.get("trace"),
    )
    return result


async def _answer(
    question: str,
    *,
    session: AsyncSession,
    store: VectorStore,
    provider: LLMProvider,
    reranker: Reranker | None,
    inspect: bool,
    started_unix: float | None,
    request_id: str,
) -> ChatResult:
    settings = get_settings()
    started = time.perf_counter() if started_unix is None else started_unix
    stages_ms: dict[str, int] = {}
    trace: dict[str, Any] = {}

    if reranker is None:
        from app.retrieval.reranker import get_reranker

        reranker = get_reranker()

    # ── 1. analyse ---------------------------------------------------------
    t0 = time.perf_counter()
    analysis = await analyze_question(question, provider=provider)
    stages_ms["analyze_ms"] = int((time.perf_counter() - t0) * 1000)
    query_analysis_payload = {
        "original": analysis.original_question,
        "normalized": analysis.original_question,
        "detected_product": analysis.detected_product,
        "detected_category": analysis.detected_category,
        "intent": analysis.intent,
        "entities": analysis.entities,
        "confidence": {
            "product": analysis.classification_confidence.get("product", 0.0),
            "category": analysis.classification_confidence.get("category", 0.0),
            "intent": analysis.classification_confidence.get("intent", 0.0),
        },
        "rewrite_queries": rewrite_queries(question, analysis),
        "ambiguity_note": analysis.ambiguity_note,
    }

    # ── 2. retrieve --------------------------------------------------------
    t0 = time.perf_counter()
    rewrites = rewrite_queries(question, analysis)
    all_candidates = []
    plan = build_metadata_filter(analysis)
    widened = False
    for rq in rewrites:
        found = await retrieve(rq, session=session, store=store, analysis=analysis)
        if should_widen(plan, found):
            # A confident filter that matches nothing is a filter that hid the
            # evidence, and FR-010's reasoning — a too-narrow filter silently
            # removes what a good answer needs — does not care how sure the
            # classifier was. So the search is repeated once, wide, and the
            # response reports the widened scope rather than the intended one.
            widened = True
            plan = build_metadata_filter(None)
            found = await retrieve(rq, session=session, store=store, analysis=None)
        all_candidates.extend(found)
    stages_ms["retrieve_ms"] = int((time.perf_counter() - t0) * 1000)
    applied_filters = {} if widened or plan.suppressed else (plan.filter or {})

    # ── 3. rerank ----------------------------------------------------------
    t0 = time.perf_counter()
    rerank_input = [
        RerankCandidate(
            id=c.id,
            text=c.text,
            retrieval_score=c.retrieval_score,
            metadata=c.metadata,
        )
        for c in all_candidates
    ]
    reranked: list[RerankedHit] = await reranker.rerank(
        query=question,
        candidates=rerank_input,
        top_n=settings.retrieval_rerank_top_n,
    )
    stages_ms["rerank_ms"] = int((time.perf_counter() - t0) * 1000)

    # ── 4. select evidence ---------------------------------------------------
    t0 = time.perf_counter()
    evidence = select_evidence(
        question,
        reranked,
        min_units=max(1, settings.evidence_min_units or 3),
        max_units=min(settings.evidence_max_units or 6, 6),
    )
    stages_ms["select_ms"] = int((time.perf_counter() - t0) * 1000)

    # ── 4b. the quality gate (FR-007) ------------------------------------------
    # The refusal lives here, *before* generation, which is the point: a pool
    # that cannot support an answer is never shown to the model, so there is
    # nothing for the model to be tempted into filling.
    decision = gate_evidence(reranked, thresholds=settings, selected=evidence)
    if not evidence or decision.refused:
        return _refused(
            request_id=request_id,
            question=question,
            reason="INSUFFICIENT_EVIDENCE",
            searched={
                "queries": rewrites,
                "products": searched_products(analysis),
                "applied_filters": applied_filters,
                "candidates_retrieved": len(all_candidates),
                "candidates_reranked": len(reranked),
                "evidence_selected": len(evidence),
            },
            leads=build_leads(reranked, limit=LEAD_LIMIT),
            total_ms=int((time.perf_counter() - started) * 1000),
            trace=trace if inspect else None,
            query_analysis=query_analysis_payload,
            stages_ms=stages_ms,
        )

    # ── 5. generate, verify, and never force -------------------------------
    t0 = time.perf_counter()
    attempts = 0
    generated = None
    verification = None
    provider_error: str | None = None
    while attempts < settings.generation_max_attempts:
        attempts += 1
        try:
            generated = await generate_answer(question, evidence=evidence, provider=provider)
        except ProviderError as exc:
            provider_error = str(exc)
            break
        if generated.answerable:
            raw_evidence_text = "\n\n".join(u.hit.text for u in evidence)
            verification = await verify_answer(
                generated.answer,
                evidence_text=raw_evidence_text,
                provider=provider,
            )
            if verification.classification == "SUPPORTED":
                break
        else:
            verification = None
            break

    ver_ms = int((time.perf_counter() - t0) * 1000)
    stages_ms["generate_ms"] = ver_ms

    if provider_error is not None or generated is None:
        if inspect:
            # A refusal whose cause is invisible is a support ticket. The text is
            # the provider's own message, never the user's question and never
            # anything from the corpus, so it is safe to show to the operator who
            # asked for inspection and never part of a normal response.
            trace["generation"] = {
                "attempts": attempts,
                "verification": None,
                "provider_error": provider_error,
            }
        return _refused(
            request_id=request_id,
            question=question,
            reason="GENERATION_FAILED",
            searched={
                "queries": rewrites,
                "products": searched_products(analysis),
                "applied_filters": applied_filters,
                "candidates_retrieved": len(all_candidates),
                "candidates_reranked": len(reranked),
                "evidence_selected": len(evidence),
            },
            leads=[],
            total_ms=int((time.perf_counter() - started) * 1000),
            trace=trace if inspect else None,
            query_analysis=query_analysis_payload,
            stages_ms=stages_ms,
        )

    if (verification is None and not generated.answerable) or (
        verification is not None and verification.classification == "UNSUPPORTED"
    ):
        # The model said the evidence does not support an answer, and the
        # verifier agreed after the cap: the honest response is a refusal, not a
        # softly-phrased paragraph that still reads as an answer.
        return _refused(
            request_id=request_id,
            question=question,
            reason="INSUFFICIENT_EVIDENCE",
            searched={
                "queries": rewrites,
                "products": searched_products(analysis),
                "applied_filters": applied_filters,
                "candidates_retrieved": len(all_candidates),
                "candidates_reranked": len(reranked),
                "evidence_selected": len(evidence),
            },
            leads=[
                {
                    "source_url": u.hit.metadata.get("source_url") or "",
                    "title": u.hit.metadata.get("title") or "",
                    "heading_path": u.heading_path,
                    "relevance": round(u.rerank_score, 3),
                    "insufficient": True,
                }
                for u in evidence[:3]
            ],
            total_ms=int((time.perf_counter() - started) * 1000),
            trace=trace if inspect else None,
            query_analysis=query_analysis_payload,
            stages_ms=stages_ms,
        )

    # ── 6. validate citations ------------------------------------------------
    evidence_sources = [
        EvidenceSource(
            evidence_id=u.hit.id,
            document_id=u.document_id,
            unit_id=u.unit_id or u.hit.metadata.get("unit_id", ""),
            source_url=u.hit.metadata.get("source_url", ""),
            title=u.hit.metadata.get("title", ""),
            product=u.hit.metadata.get("product"),
            category=u.hit.metadata.get("category"),
            heading_path=list(u.heading_path),
            quote=u.hit.text[:200].strip() or None,
            retrieval_score=u.retrieval_score,
            rerank_score=u.rerank_score,
        )
        for u in evidence
    ]
    # Inline markers first, then the model's own citation list. Both are resolved
    # against what was served; the union is what the answer actually leans on, and
    # the marker order is what puts `[1]` on the first claim the reader sees.
    served_ids = [u.hit.id for u in evidence]
    markers = resolve_answer_markers(generated.answer, served_ids)
    cited_ids = list(
        dict.fromkeys([*markers.cited_ids, *(c.evidence_id for c in generated.citations)])
    )

    granularity = citation_granularity(markers, cited_ids)

    if granularity == "none":
        # An answer whose claims point at nothing is the one failure FR-002 exists
        # to prevent, and it is checked here rather than trusted: the model's own
        # `citations` array can name served ids the prose never leans on, and a
        # reader cannot check a claim against a list they were shown no link from.
        # Refused, not shipped with a citation list bolted on afterwards.
        #
        # The *weaker* case — sources named, claims not marked inline — is
        # answered, with the difference recorded in the trace. Measured live: the
        # model marks claims in about half its answers, and refusing the rest would
        # refuse half of all answerable questions over a formatting habit while
        # their citations resolved exactly as well.
        return _refused(
            request_id=request_id,
            question=question,
            reason="GENERATION_FAILED",
            searched={
                "queries": rewrites,
                "products": searched_products(analysis),
                "applied_filters": applied_filters,
                "candidates_retrieved": len(all_candidates),
                "candidates_reranked": len(reranked),
                "evidence_selected": len(evidence),
            },
            leads=[],
            total_ms=int((time.perf_counter() - started) * 1000),
            trace=trace if inspect else None,
            query_analysis=query_analysis_payload,
            stages_ms=stages_ms,
        )

    validation = validate_citations(
        cited_ids,
        retrieved=evidence_sources,
        cited_links=[
            {"evidence_id": c.evidence_id, "source_url": c.source_url} for c in generated.citations
        ],
    )

    citations_payload = [
        {
            "rank": i + 1,
            "document_id": c.document_id,
            "unit_id": c.unit_id,
            "source_url": c.source_url,
            "title": c.title,
            "product": c.product,
            "category": c.category,
            "heading_path": c.heading_path,
            "quote": c.quote or "",
            "retrieval_score": c.retrieval_score,
            "rerank_score": c.rerank_score,
            "validation_state": c.validation_state,
            "validation_note": c.validation_note,
        }
        for i, c in enumerate(validation.citations)
    ]

    if inspect:
        trace["citations"] = {
            **trace.get("citations", {}),
            "unresolved_markers": markers.rejected,
            # `per_claim` or `list`: whether each claim named its own source, or
            # the sources were named once at the end. Recorded either way.
            "granularity": granularity,
        }
        trace["generation"] = {
            "attempts": attempts,
            "verification": verification.classification if verification else None,
            "verified_reason": verification.reason if verification else None,
        }
        trace["citations"] = {
            "valid": sum(1 for c in validation.citations if c.validation_state == "valid"),
            "stripped": validation.stripped,
            "repaired": validation.repaired,
            "rejected_identifiers": validation.rejected_identifiers,
        }

    payload = {
        "request_id": request_id,
        "question": question,
        "outcome": "answered",
        "answer": markers.text,
        "citations": citations_payload,
        "timing": {"total_ms": int((time.perf_counter() - started) * 1000), "stages_ms": stages_ms},
        "query_analysis": query_analysis_payload,
    }
    if trace:
        payload["trace"] = trace

    return ChatResult(payload=payload, stages_ms=stages_ms)


__all__ = ["ChatResult", "run_chat"]
