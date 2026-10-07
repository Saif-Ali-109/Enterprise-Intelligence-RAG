"""The evaluation run (T144–T146): same service, real questions, persisted rows.

**The runner executes the same chat service a user would hit.** It is tempting
to grade a "retriever directly" when only retrieval metrics matter, but a run
whose retrieval path differs from the one whose answer-quality metrics are
taken in the same row measures a pipeline that does not exist (R-007). The
trace a run reads is the one `run_chat(..., inspect=True)` persisted — the
same join point a troubleshooter uses.

**Aggregates are means over per-question rows, and absent means not applied.**
A metric column that averaged in a fabricated 0.0 for "not applicable" is a
number that never happened (Principle VI). Unsupported questions carry
`null` retrieval metrics for exactly that reason, and the run-level columns
mean over the questions where a number exists.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.models import Document, DocumentUnit, EvaluationResult, EvaluationRun
from app.db.session import get_session_factory
from app.evaluation import metrics as m
from app.evaluation.dataset import by_id, dataset_fingerprint, load_dataset
from app.evaluation.metrics import PerformanceStats

__all__ = [
    "RunInProgress",
    "create_run",
    "execute_run",
    "gate_outcome",
]

#: Threshold key → EvaluationRun.metrics key (names differ once: the config
#: names the gate, the contract names the metric).
_THRESHOLD_TO_METRIC = {
    "recall_at_5": "recall_at_5",
    "precision_at_5": "precision_at_5",
    "mrr": "mrr",
    "cross_product_coverage": "cross_product_domain_coverage",
    "citation_validity": "citation_validity",
    "claim_coverage": "claim_coverage",
    "faithfulness": "faithfulness",
    "unsupported_refusal_rate": "unsupported_refusal_rate",
    "p95_latency_ms": "p95_latency_ms",
}


class RunInProgress(Exception):
    pass


async def create_run(session: AsyncSession) -> EvaluationRun:
    """Refuse a second concurrent run, load and validate the dataset, and
    persist the queued row.

    A second run is refused for the same reason a second crawl of the same
    frontier is: two writers over the same corpus counts and thresholds would
    make a run's numbers incomparable to themselves.
    """
    active = (
        await session.execute(
            select(func.count(EvaluationRun.id)).where(
                EvaluationRun.status.in_(("queued", "running"))
            )
        )
    ).scalar_one()
    if active:
        raise RunInProgress("an evaluation run is already in progress")

    dataset, raw = load_dataset()
    fingerprint = dataset_fingerprint(raw)
    settings = get_settings()

    # The registry table mirrors the committed gold set: `evaluation_results`
    # keys off question ids, and the join only means something if the rows are
    # the ones the fingerprint just validated. Upsert keeps the mirror idempotent
    # across runs of different versions.
    from app.db.models import EvaluationQuestion

    for q in dataset.questions:
        existing = await session.get(EvaluationQuestion, q.id)
        values = {
            "question": q.question,
            "category": q.category,
            "difficulty": q.difficulty,
            "is_unsupported": q.is_unsupported,
            "expected_product": q.expected.product,
            "expected_category": q.expected.category,
            "expected_heading_path": list(q.expected.heading_path),
            "expected_topics": list(q.expected.topics),
            "answer_notes": q.expected.notes,
            "tags": next((rq.get("tags", []) for rq in raw["questions"] if rq["id"] == q.id), []),
            "dataset_version": dataset.dataset_version,
        }
        if existing is None:
            session.add(EvaluationQuestion(id=q.id, **values))
        else:
            for key, value in values.items():
                setattr(existing, key, value)

    document_count = (await session.execute(select(func.count(Document.id)))).scalar_one()
    unit_count = (await session.execute(select(func.count(DocumentUnit.id)))).scalar_one()

    run = EvaluationRun(
        dataset_version=dataset.dataset_version,
        dataset_fingerprint=fingerprint,
        config_snapshot=settings.public_config(),
        question_count=len(dataset.questions),
        metrics={},
        thresholds=settings.evaluation_thresholds(),
        gate_outcome="fail",
        gate_failures=[],
        corpus_document_count=document_count,
        corpus_unit_count=unit_count,
        status="queued",
    )
    session.add(run)
    await session.commit()
    await session.refresh(run)
    return run


def gate_outcome(
    metrics: dict[str, Any], thresholds: dict[str, float]
) -> tuple[str, list[dict[str, Any]]]:
    """`pass` or `fail`, never soft. The two zero-count gates are absolute and
    are checked outside the configured thresholds, as counts (FR-065, FR-066)."""
    failures: list[dict[str, Any]] = []

    fabricated = metrics.get("fabricated_fact_count")
    if fabricated is not None and fabricated > 0:
        failures.append({"gate": "fabricated_fact_count", "target": 0, "actual": fabricated})
    invalid = metrics.get("invalid_citation_count")
    if invalid is not None and invalid > 0:
        failures.append({"gate": "invalid_citation_count", "target": 0, "actual": invalid})

    for gate, metric_name in _THRESHOLD_TO_METRIC.items():
        if gate not in thresholds:
            continue
        target = thresholds[gate]
        actual = metrics.get(metric_name)
        if actual is None:
            # Not applicable to any question: the contract's failure entry
            # requires a numeric `actual`, so the honest record of "not
            # measured" is the null metric itself, serialised on the run — not
            # a synthetic zero bolted onto a failure row.
            continue
        if gate == "p95_latency_ms":
            if actual > target:
                failures.append({"gate": metric_name, "target": target, "actual": actual})
        elif gate == "cross_product_coverage":
            if actual < target:
                failures.append({"gate": metric_name, "target": target, "actual": actual})
        else:
            if actual < target:
                failures.append({"gate": metric_name, "target": target, "actual": actual})
    return ("pass" if not failures else "fail"), failures


async def execute_run(
    run_id: uuid.UUID,
    *,
    store: Any,
    provider: Any,
    dataset_path: Any = None,
    now: Callable[[], float] | None = None,
) -> None:
    """Drive the run row to a terminal status. A synchronous engine call per
    question would be impatient; the per-question budget is the same as for a
    user question (R-007)."""
    from app.chat.service import run_chat

    factory = get_session_factory()
    clock = now or time.perf_counter

    async with factory() as session:
        run = await session.get(EvaluationRun, run_id)
        if run is None:
            return
        run.status = "running"
        await session.commit()

        dataset, raw = load_dataset(dataset_path)
        questions = by_id(dataset)

        perf = PerformanceStats()
        per_question: list[dict[str, Any]] = []
        cross_flags: list[bool] = []
        unsupported_refused = 0
        unsupported_total = 0

        for q in dataset.questions:
            started = clock()
            error: str | None = None
            payload: dict[str, Any] | None = None
            try:
                chat_result = await run_chat(
                    q.question,
                    session=session,
                    store=store,
                    provider=provider,
                    inspect=True,
                    emit=None,
                )
                payload = chat_result.payload
            except Exception as exc:  # noqa: BLE001 - one question never sinks the run
                error = f"{type(exc).__name__}: {exc}"[:500]
            latency_ms = int((clock() - started) * 1000)
            perf.total += 1
            perf.latencies_ms.append(latency_ms)
            if error is not None:
                perf.errors += 1

            outcome = _grade_question(q, questions, payload, latency_ms, error, per_question)
            await _record_row(session, run_id, q, outcome)
            if error is None and payload is not None:
                if q.category == "cross_product":
                    # `searched` is populated on the refusal path; an answered
                    # response carries the evidence instead. The products this
                    # question actually consulted are therefore read from
                    # whichever of the two the response has — a cross-product
                    # gate that only counted refusals would report coverage for
                    # exactly the questions that produced no answer.
                    products = _searched_products(payload)
                    cross_flags.append(len(products) >= 2)
                if q.is_unsupported:
                    unsupported_total += 1
                    if payload.get("outcome") == "refused":
                        unsupported_refused += 1

        metrics = _aggregate(
            per_question, cross_flags, unsupported_refused, unsupported_total, perf
        )
        # Re-read after the per-question commits: those expire the ORM instance,
        # and touching an expired attribute outside a greenlet context raises
        # MissingGreenlet rather than the value. Copying the thresholds into a
        # plain dict here is what the run is judged against.
        await session.refresh(run)
        outcome_gate, failures = gate_outcome(metrics, dict(run.thresholds))

        run.metrics = metrics
        run.gate_outcome = outcome_gate
        run.gate_failures = failures
        run.status = "completed"
        run.finished_at = datetime.now(UTC)
        await session.commit()


async def _record_row(
    session: AsyncSession, run_id: uuid.UUID, q: Any, outcome: dict[str, Any]
) -> None:
    session.add(
        EvaluationResult(
            run_id=run_id,
            question_id=q.id,
            recall_at_5=outcome["recall"],
            precision_at_5=outcome["precision"],
            mrr=outcome["mrr"],
            citation_validity=outcome["citation_validity"],
            claim_coverage=outcome["claim_coverage"],
            faithfulness=outcome["faithfulness"],
            refused=outcome["refused"],
            fabricated_fact_count=outcome["fabricated_fact_count"],
            invalid_citation_count=outcome["invalid_citation_count"],
            latency_ms=outcome["latency_ms"],
            error=outcome["error"],
        )
    )
    await session.commit()


def _grade_question(
    q: Any,
    questions: dict[str, Any],
    payload: dict[str, Any] | None,
    latency_ms: int,
    error: str | None,
    per_question: list[dict[str, Any]],
) -> dict[str, Any]:
    expected = q.expected
    result: dict[str, Any] = {
        "recall": None,
        "precision": None,
        "mrr": None,
        "citation_validity": None,
        "claim_coverage": None,
        "faithfulness": None,
        "refused": False,
        "fabricated_fact_count": 0,
        "invalid_citation_count": 0,
        "latency_ms": latency_ms,
        "error": error,
    }
    if payload is not None:
        trace = payload.get("trace") or {}
        retrieval = trace.get("retrieval") or {}
        reranking = trace.get("reranking") or {}
        citations_trace = trace.get("citations") or {}

        candidates = _ranked_candidates(retrieval, reranking)
        recall, precision, mrr = m.retrieval_grades(
            candidates, expected=expected, is_unsupported=q.is_unsupported
        )
        result.update(recall=recall, precision=precision, mrr=mrr)

        validation_counts = {
            "valid": citations_trace.get("valid", 0),
            "stripped": citations_trace.get("stripped", 0),
            "rejected_identifiers": len(citations_trace.get("rejected_identifiers", []) or []),
        }
        result["citation_validity"] = m.citation_validity(validation_counts)
        result["invalid_citation_count"] = (
            validation_counts["stripped"] + validation_counts["rejected_identifiers"]
        )

        answer = payload.get("answer") or ""
        result["refused"] = payload.get("outcome") == "refused"
        if not result["refused"] and answer:
            topics = list(expected.topics)
            result["claim_coverage"] = m.claim_coverage(topics, answer)
            quotes = [c.get("quote", "") for c in payload.get("citations", [])]
            result["faithfulness"] = m.faithfulness(answer, quotes)
            result["fabricated_fact_count"] = m.fabricated_facts(answer)
        else:
            result["claim_coverage"] = None
            result["faithfulness"] = None

    per_question.append(result | {"category": q.category, "is_unsupported": q.is_unsupported})
    return result


def _searched_products(payload: dict[str, Any]) -> set[str]:
    """The products this question consulted, from either response shape."""
    searched = payload.get("searched") or {}
    products = {p for p in (searched.get("products") or []) if p}
    if products:
        return products
    trace = payload.get("trace") or {}
    for candidate in (trace.get("retrieval") or {}).get("candidates", []) or []:
        if candidate.get("product"):
            products.add(str(candidate["product"]))
    for unit in (trace.get("citations") or {}).get("evidence", []) or []:
        if unit.get("product"):
            products.add(str(unit["product"]))
    return products


def _ranked_candidates(
    retrieval: dict[str, Any], reranking: dict[str, Any]
) -> list[dict[str, Any]]:
    by_id = {c.get("id"): c for c in retrieval.get("candidates", [])}
    ranked = []
    for r in reranking.get("candidates", []) or []:
        base = by_id.get(r.get("id"), {})
        ranked.append({**base, **r})
    if not ranked:
        ranked = list(retrieval.get("candidates", []) or [])
    ranked.sort(key=lambda c: (c.get("rank") is None, c.get("rank") or 0))
    return ranked


def _aggregate(
    per_question: list[dict[str, Any]],
    cross_flags: list[bool],
    unsupported_refused: int,
    unsupported_total: int,
    perf: PerformanceStats,
) -> dict[str, Any]:
    def col(key: str) -> list[float | None]:
        return [p[key] for p in per_question]

    return {
        "recall_at_5": m.mean(col("recall")),
        "precision_at_5": m.mean(col("precision")),
        "mrr": m.mean(col("mrr")),
        "cross_product_domain_coverage": (
            sum(cross_flags) / len(cross_flags) if cross_flags else None
        ),
        "citation_validity": m.mean(col("citation_validity")),
        "claim_coverage": m.mean(col("claim_coverage")),
        "faithfulness": m.mean(col("faithfulness")),
        "unsupported_refusal_rate": (
            unsupported_refused / unsupported_total if unsupported_total else None
        ),
        "fabricated_fact_count": sum(p["fabricated_fact_count"] for p in per_question),
        "invalid_citation_count": sum(p["invalid_citation_count"] for p in per_question),
        "p50_latency_ms": perf.p50(),
        "p95_latency_ms": perf.p95(),
        "error_rate": perf.error_rate(),
    }
