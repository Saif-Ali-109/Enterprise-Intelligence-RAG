"""The audit record for one question (FR-048, FR-033, FR-047).

Every question this system answers, refuses, or fails on writes one
`query_logs` row. The row's primary key **is** the `request_id` returned to the
client, so the value a user quotes in a bug report is the value that joins the
report to the record — nothing to translate, and no way for the two to drift.

Three properties the recorder holds, each of which is a way an audit log becomes
worthless:

**It never raises into the request.** An answer that was correct does not become
a 500 because a JSONB write failed, and a refusal does not become an outage. A
failure to record is logged at error level with the request id, which is the
signal that the audit trail has a hole in it — loud, and not on the user's path.

**It stores the counts that make a complaint investigable.** A row with only the
question in it cannot answer "was that a refusal because nothing was indexed, or
because the retrieval found nothing?" — the counts and the filters can (FR-048).

**The trace is stored only when inspection was requested.** `pipeline_trace` is
null otherwise, because a trace is per-request operator-facing detail (FR-033)
and a permanent copy of it for every question is a data-retention decision
nobody made. No reasoning is ever stored; the trace has no field for it.

**Bounded retention is a separate concern** and lives in the purge, not here:
this recorder writes, and does not decide how long anything survives.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.models import QueryLog

_log = get_logger("chat.audit")


async def record_query(
    session: AsyncSession,
    *,
    request_id: str,
    question: str,
    outcome: str,
    answer: str | None = None,
    refusal_reason: str | None = None,
    detected_product: str | None = None,
    detected_category: str | None = None,
    intent: str | None = None,
    classification_confidence: dict[str, Any] | None = None,
    rewrite_queries: list[str] | None = None,
    applied_filters: dict[str, Any] | None = None,
    candidates_retrieved: int = 0,
    candidates_reranked: int = 0,
    evidence_selected: int = 0,
    stage_latency_ms: dict[str, int] | None = None,
    total_latency_ms: int = 0,
    provider: str | None = None,
    model: str | None = None,
    token_usage: dict[str, Any] | None = None,
    rerank_model: str | None = None,
    pipeline_trace: dict[str, Any] | None = None,
    error_code: str | None = None,
) -> None:
    """Write one `query_logs` row. Never raises.

    The commit is its own transaction boundary and the caller's session is
    reused rather than replaced: the request's read session is the one already
    open, so the row joins the same unit of work the answer was assembled in.
    """
    try:
        row = QueryLog(
            id=uuid.UUID(request_id),
            question=question,
            outcome=outcome,
            answer=answer,
            refusal_reason=refusal_reason,
            detected_product=detected_product,
            detected_category=detected_category,
            intent=intent,
            classification_confidence=classification_confidence,
            rewrite_queries=list(rewrite_queries or []),
            applied_filters=dict(applied_filters or {}),
            candidates_retrieved=candidates_retrieved,
            candidates_reranked=candidates_reranked,
            evidence_selected=evidence_selected,
            stage_latency_ms=dict(stage_latency_ms or {}),
            total_latency_ms=total_latency_ms,
            provider=provider,
            model=model,
            token_usage=token_usage,
            rerank_model=rerank_model,
            pipeline_trace=pipeline_trace,
            error_code=error_code,
        )
        session.add(row)
        await session.commit()
    except Exception as exc:  # noqa: BLE001 - the request must not fail because audit did
        _log.error(
            "the audit record could not be written; this request left no query_logs row",
            extra={"request_id": request_id, "error": str(exc)[:200]},
        )
        await session.rollback()


__all__ = ["record_query"]
