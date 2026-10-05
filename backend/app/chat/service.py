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
from dataclasses import dataclass, field, replace
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.api.events import EventEmitter, EventName
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
from app.retrieval.query_rewriter import base_question, rewrite_queries, scope_of
from app.retrieval.reranker import RerankCandidate, RerankedHit, Reranker
from app.retrieval.retriever import Candidate, build_metadata_filter, retrieve, should_widen
from app.retrieval.vector_store import VectorStore

#: How many weakly related sources a refusal may offer (FR-008). Three, because
#: a list of ten is a search result page, not a hint.
LEAD_LIMIT = 3


def _analysis_for_scope(analysis: QueryAnalysis, scope: str | None) -> QueryAnalysis:
    """The analysis as it applies to one aspect's domain.

    A cross-product question's own analysis suppresses the product filter, which
    is right for the question and wrong for one of its aspects: each aspect wants
    exactly one domain's evidence. So the scope replaces the filter decision, and
    the ambiguity note goes with it — an aspect is not ambiguous, it is specific.
    """
    if scope is None:
        return analysis
    return replace(
        analysis,
        detected_product=scope,
        named_products=[scope],
        ambiguity_note=None,
        classification_confidence={
            **analysis.classification_confidence,
            # Certain, and not because anything was classified: the product name
            # is in the user's own sentence. Gating this on the classifier's
            # confidence would suppress the filter for the very aspects that most
            # need one — the cross-product question whose filter was suppressed in
            # the first place.
            "product": 1.0,
        },
    )


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


def _stream_refusal_through(emit: EventEmitter | None) -> None:
    """Emit the events a refusal skipped, with zero counts, **through event 7**.

    events.md §3: no event is ever skipped within 1–7, and a stage that produced
    nothing still reports with a zero count. Every early return in the pipeline
    therefore goes through here, because the alternative — remembering to emit
    placeholders per branch — is how a stream ends with four events and a client
    that waits forever.

    **Event 8 is not included, and that is load-bearing.** It is emitted by
    `_refused`, which is the only place that knows the refusal's payload. An
    earlier version of this helper emitted `answer_completed` as a placeholder and
    `_refused` then emitted it again, which the emitter refused — correctly. Two
    emitters for one event is the bug the order check exists to catch.
    """
    if emit is not None:
        emit.emit_skipped_through(EventName.CITATION_VALIDATION)


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
    emit: EventEmitter | None = None,
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
    if emit is not None:
        emit.emit(
            EventName.ANSWER_COMPLETED,
            outcome=payload["outcome"],
            answer=None,
            refusal_reason=payload["refusal_reason"],
            searched=payload["searched"],
            leads=payload["leads"],
            citations=[],
            total_ms=total_ms,
        )
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
    emit: EventEmitter | None = None,
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
            emit=emit,
        )
    except AppError as exc:
        if emit is not None and not emit.terminal_emitted:
            # `error` is terminal and replaces `answer_completed`, which is why it
            # may arrive here with events still outstanding (events.md §3).
            emit.emit(
                EventName.ERROR,
                code=exc.code.value,
                message=str(exc),
            )
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
    emit: EventEmitter | None,
) -> ChatResult:
    settings = get_settings()
    started = time.perf_counter() if started_unix is None else started_unix
    stages_ms: dict[str, int] = {}
    trace: dict[str, Any] = {}

    if reranker is None:
        from app.retrieval.reranker import get_reranker

        reranker = get_reranker()

    if emit is not None:
        emit.emit(EventName.QUERY_RECEIVED, question=question, inspect=inspect)

    # ── 1. analyse ---------------------------------------------------------
    t0 = time.perf_counter()
    analysis = await analyze_question(question, provider=provider)
    stages_ms["analyze_ms"] = int((time.perf_counter() - t0) * 1000)
    # Computed once and shared: the response payload, the `query_analyzed` event,
    # and the retrieval loop must all report the *same* queries, and a rewriter
    # called twice is a rewriter that may be called twice differently.
    rewritten: list[str] = rewrite_queries(question, analysis)
    query_analysis_payload: dict[str, Any] = {
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
        "rewrite_queries": rewritten,
        "ambiguity_note": analysis.ambiguity_note,
    }

    plan = build_metadata_filter(analysis)
    if emit is not None:
        emit.emit(
            EventName.QUERY_ANALYZED,
            detected_product=analysis.detected_product,
            detected_category=analysis.detected_category,
            intent=analysis.intent,
            entities=analysis.entities,
            classification_confidence=query_analysis_payload["confidence"],
            rewrite_queries=rewritten,
            applied_filters=plan.filter or {},
            filters_suppressed=plan.suppressed,
            ambiguity_note=analysis.ambiguity_note,
        )
        emit.emit(
            EventName.RETRIEVAL_STARTED,
            candidate_pool=settings.retrieval_candidate_pool,
            query_count=len(rewritten),
        )

    # ── 2. retrieve --------------------------------------------------------
    t0 = time.perf_counter()
    rewrites = rewritten
    all_candidates: list[Candidate] = []
    widened = False
    queries_executed: list[dict[str, Any]] = []
    per_query_filters: dict[str, dict[str, Any]] = {}
    seen_candidates: set[str] = set()

    for rq in rewrites:
        # Each scoped query searches its own domain (T115 / SC-004). The scope
        # lives in a metadata filter, never in the embedded text: repeating a
        # product name in a similarity query biases the ranking toward units that
        # merely mention it, which is not the same as units that are about it.
        scope = scope_of(rq)
        aspect_analysis = _analysis_for_scope(analysis, scope)
        aspect_plan = build_metadata_filter(aspect_analysis)
        found = await retrieve(
            base_question(rq), session=session, store=store, analysis=aspect_analysis
        )
        if should_widen(aspect_plan, found):
            # A confident filter that matches nothing is a filter that hid the
            # evidence, and FR-010's reasoning — a too-narrow filter silently
            # removes what a good answer needs — does not care how sure the
            # classifier was. So the search is repeated once, wide, and the
            # response reports the widened scope rather than the intended one.
            widened = True
            aspect_plan = build_metadata_filter(None)
            found = await retrieve(base_question(rq), session=session, store=store, analysis=None)
        per_query_filters[rq] = aspect_plan.filter or {}
        queries_executed.append(
            {
                "query": rq,
                "returned": len(found),
                "top_score": max((c.retrieval_score for c in found), default=0.0),
            }
        )
        if aspect_plan.suppressed:
            widened = widened or plan.suppressed or True
        for candidate in found:
            # Merged and deduplicated across aspects, and the *pool* budget is
            # still respected: two aspects do not buy 24 candidates.
            if len(all_candidates) >= settings.retrieval_candidate_pool:
                break
            if candidate.id in seen_candidates:
                continue
            seen_candidates.add(candidate.id)
            all_candidates.append(candidate)
    stages_ms["retrieve_ms"] = int((time.perf_counter() - t0) * 1000)
    applied_filters = {} if widened or plan.suppressed else (plan.filter or {})
    if emit is not None:
        emit.emit(
            EventName.RETRIEVAL_COMPLETED,
            candidates_retrieved=len(all_candidates),
            queries_executed=queries_executed,
        )

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
    if emit is not None:
        emit.emit(
            EventName.RERANKING_COMPLETED,
            candidates_reranked=len(reranked),
            rerank_model=settings.pinecone_rerank_model,
        )

    # ── 4. select evidence ---------------------------------------------------
    t0 = time.perf_counter()
    evidence = select_evidence(
        question,
        reranked,
        min_units=max(1, settings.evidence_min_units or 3),
        max_units=min(settings.evidence_max_units or 6, 6),
        # T116 / SC-004: a question that spans domains reserves a slot per domain,
        # so the answer cannot be assembled from whichever one scores highest.
        required_groups=(
            {product: product for product in analysis.named_products if product}
            if analysis.ambiguity_note
            else None
        ),
        # A single-domain question is answered from its own domain first (T112),
        # unless the widening path above found evidence nowhere else.
        prefer_group=None if analysis.ambiguity_note else analysis.detected_product,
    )
    stages_ms["select_ms"] = int((time.perf_counter() - t0) * 1000)

    # ── 4b. the quality gate (FR-007) ------------------------------------------
    # The refusal lives here, *before* generation, which is the point: a pool
    # that cannot support an answer is never shown to the model, so there is
    # nothing for the model to be tempted into filling.
    decision = gate_evidence(reranked, thresholds=settings, selected=evidence)
    if not evidence or decision.refused:
        _stream_refusal_through(emit)
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
            emit=emit,
        )

    # ── 5. generate, verify, and never force -------------------------------
    t0 = time.perf_counter()
    attempts = 0
    generated = None
    verification = None
    provider_error: str | None = None
    while attempts < settings.generation_max_attempts:
        attempts += 1
        if emit is not None:
            # A second `generation_started` is the only signal that a retry
            # happened (events.md §4), which is why it is emitted per attempt
            # rather than once per request.
            emit.emit(
                EventName.GENERATION_STARTED,
                evidence_count=len(evidence),
                attempt=attempts,
            )
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
        _stream_refusal_through(emit)
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
            emit=emit,
        )

    if (verification is None and not generated.answerable) or (
        verification is not None and verification.classification == "UNSUPPORTED"
    ):
        # The model said the evidence does not support an answer, and the
        # verifier agreed after the cap: the honest response is a refusal, not a
        # softly-phrased paragraph that still reads as an answer.
        _stream_refusal_through(emit)
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
            emit=emit,
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
        _stream_refusal_through(emit)
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
            emit=emit,
        )

    validation = validate_citations(
        cited_ids,
        retrieved=evidence_sources,
        cited_links=[
            {"evidence_id": c.evidence_id, "source_url": c.source_url} for c in generated.citations
        ],
    )

    if emit is not None:
        emit.emit(
            EventName.CITATION_VALIDATION,
            citations_valid=sum(1 for c in validation.citations if c.validation_state == "valid"),
            citations_stripped=validation.stripped,
            citations_repaired=validation.repaired,
            rejected_identifiers=[*markers.rejected, *validation.rejected_identifiers],
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

    payload: dict[str, Any] = {
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

    if emit is not None:
        emit.emit(
            EventName.ANSWER_COMPLETED,
            outcome=payload["outcome"],
            answer=payload["answer"],
            citations=citations_payload,
            total_ms=payload["timing"]["total_ms"],
            confidence=None,
        )

    return ChatResult(payload=payload, stages_ms=stages_ms)


__all__ = ["ChatResult", "run_chat"]
