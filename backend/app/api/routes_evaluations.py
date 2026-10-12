"""Evaluation endpoints (T148): the dataset, runs, and per-question results.

A run is started asynchronously because a 30-plus question suite cannot be
served in a request's life (R-007). Progress is polled on the run resource:
`status` moves queued → running → completed/failed, and `metrics` stays
`null` until a finished run has real numbers (FR-036). Against that rule, a
run listing that always shows an empty collection is correct behaviour — it is
"no run yet", never "all zeros" (FR-037).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from functools import partial

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy import func, select

from app.api.deps import DEFAULT_LIMIT, Session
from app.core.errors import ResourceNotFound
from app.core.ratelimit import enforce_general
from app.db.models import EvaluationResult, EvaluationRun
from app.evaluation.dataset import load_dataset
from app.evaluation.runner import RunInProgress, create_run, execute_run
from app.generation.provider import get_language_model
from app.retrieval.vector_store import get_vector_store
from app.schemas.evaluations import (
    EvaluationDataset,
    EvaluationResultPage,
    EvaluationRunPage,
)
from app.schemas.evaluations import (
    EvaluationResult as EvaluationResultOut,
)
from app.schemas.evaluations import (
    EvaluationRun as EvaluationRunOut,
)

router = APIRouter(tags=["evaluations"])
_log = logging.getLogger("app.api.evaluations")


def _run_out(run: EvaluationRun) -> EvaluationRunOut:
    return EvaluationRunOut(
        id=str(run.id),
        dataset_version=run.dataset_version,
        dataset_fingerprint=run.dataset_fingerprint,
        status=run.status,
        started_at=run.started_at,
        finished_at=run.finished_at,
        question_count=run.question_count,
        metrics=run.metrics if run.metrics else None,
        thresholds=run.thresholds,
        gate_outcome=run.gate_outcome,
        gate_failures=run.gate_failures,
        error=run.error,
    )


def _result_out(row: EvaluationResult) -> EvaluationResultOut:
    return EvaluationResultOut(
        question_id=row.question_id,
        question=None,
        category=None,
        difficulty=None,
        recall_at_5=row.recall_at_5,
        precision_at_5=row.precision_at_5,
        mrr=row.mrr,
        citation_validity=row.citation_validity,
        claim_coverage=row.claim_coverage,
        faithfulness=row.faithfulness,
        refused=row.refused,
        fabricated_fact_count=row.fabricated_fact_count,
        invalid_citation_count=row.invalid_citation_count,
        latency_ms=row.latency_ms,
        error=row.error,
    )


@router.get(
    "/evaluations/dataset",
    response_model=EvaluationDataset,
    summary="Retrieve the curated question set",
    dependencies=[Depends(enforce_general)],
)
async def get_dataset() -> EvaluationDataset:
    dataset, _ = load_dataset()
    return dataset


@router.get(
    "/evaluations/runs",
    response_model=EvaluationRunPage,
    summary="List evaluation runs",
    description="Newest first. Empty when no run has been performed — never placeholder metrics.",
    dependencies=[Depends(enforce_general)],
)
async def list_runs(
    session: Session,
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> EvaluationRunPage:
    total = int((await session.execute(select(func.count(EvaluationRun.id)))).scalar_one())
    result = await session.execute(
        select(EvaluationRun)
        .order_by(EvaluationRun.started_at.desc(), EvaluationRun.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return EvaluationRunPage(
        items=[_run_out(r) for r in result.scalars()], total=total, limit=limit, offset=offset
    )


@router.post(
    "/evaluations/runs",
    response_model=EvaluationRunOut,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Start an evaluation run",
    description="Asynchronous; progress is polled on the run resource. 409 while a run is in progress.",
    dependencies=[Depends(enforce_general)],
)
async def start_run(session: Session, response: Response) -> EvaluationRunOut:
    try:
        run = await create_run(session)
    except RunInProgress as exc:
        from app.core.errors import StateConflict as Conflict

        raise Conflict(str(exc)) from exc
    task = asyncio.create_task(
        execute_run(run.id, store=get_vector_store(), provider=get_language_model()),
        name=f"evaluation-run-{run.id}",
    )
    # **A run whose task dies must not stay `running` (measured 2026-10-12).**
    # `POST /evaluations/runs` refuses a second run while any run is `queued` or
    # `running`, so a task that dies — a crash, a cancellation, a restart — leaves
    # the feature permanently blocked with no operator action available. Measured:
    # the backend was rebuilt four times during run `3db5cb57`, the task was
    # cancelled each time, and the run sat `running` for 100 minutes with 24 of 32
    # questions recorded and every later run refused with CONFLICT.
    #
    # The done-callback is the only place that sees a cancellation: `except
    # Exception` inside the run would not, because `CancelledError` derives from
    # `BaseException` in Python 3.8 and later. So the handler is registered on the
    # task and takes `BaseException` deliberately, recording what happened rather
    # than letting a row promise work that is no longer happening.
    task.add_done_callback(partial(_record_task_outcome, run_id=run.id))
    return _run_out(run)


def _record_task_outcome(task: asyncio.Task[None], *, run_id: uuid.UUID) -> None:
    """Mark a run failed when its task raised, was cancelled, or never started.

    Best-effort by construction: this runs in the event loop after the task
    finished, opens its own session, and swallows its own failures. An error while
    recording an error would leave the original fault invisible, which is the one
    outcome this exists to prevent.
    """
    from app.core.logging import get_logger
    from app.db.session import get_session_factory
    from app.evaluation.runner import fail_run

    log = get_logger(__name__)
    if not task.cancelled() and task.exception() is None:
        return  # the run finished normally and recorded its own outcome

    reason = (
        "the process restarted while this run was in progress"
        if task.cancelled()
        else f"the run task failed: {type(task.exception()).__name__}"
    )
    log.error("evaluation run did not finish", extra={"run_id": str(run_id), "reason": reason})

    async def _write() -> None:
        factory = get_session_factory()
        async with factory() as session:
            await fail_run(session, run_id, reason=reason)

    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_write())
    except RuntimeError:  # pragma: no cover - the loop is closing
        log.error(
            "could not record the unfinished evaluation run",
            extra={"run_id": str(run_id), "reason": reason},
        )


@router.get(
    "/evaluations/runs/{run_id}",
    response_model=EvaluationRunOut,
    summary="Retrieve an evaluation run",
    dependencies=[Depends(enforce_general)],
)
async def get_run(session: Session, run_id: uuid.UUID) -> EvaluationRunOut:
    run = await session.get(EvaluationRun, run_id)
    if run is None:
        raise ResourceNotFound(f"evaluation run {run_id} not found")
    return _run_out(run)


@router.get(
    "/evaluations/runs/{run_id}/results",
    response_model=EvaluationResultPage,
    summary="Per-question results for a run",
    dependencies=[Depends(enforce_general)],
)
async def list_results(
    session: Session,
    run_id: uuid.UUID,
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> EvaluationResultPage:
    run = await session.get(EvaluationRun, run_id)
    if run is None:
        raise ResourceNotFound(f"evaluation run {run_id} not found")
    total = int(
        (
            await session.execute(
                select(func.count(EvaluationResult.id)).where(EvaluationResult.run_id == run_id)
            )
        ).scalar_one()
    )
    result = await session.execute(
        select(EvaluationResult)
        .where(EvaluationResult.run_id == run_id)
        .order_by(EvaluationResult.question_id)
        .limit(limit)
        .offset(offset)
    )
    return EvaluationResultPage(
        items=[_result_out(r) for r in result.scalars()], total=total, limit=limit, offset=offset
    )
