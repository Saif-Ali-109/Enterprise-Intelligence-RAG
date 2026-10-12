"""A failed evaluation run must say why (FR-041).

Measured 2026-10-12: the backend was rebuilt four times during run `3db5cb57`.
Each restart cancelled the background task, the row stayed `running` for 100
minutes, and `POST /evaluations/runs` returned `CONFLICT` throughout. With the
run now reconciled to `failed`, the next question is whether an operator can tell
what happened — because `error` existed as a column and was never read, so the API
reported a failed run with no explanation at all.

This file covers the reconciliation and the exposure together, because they are
one defect seen twice: state recorded but not reported.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from app.evaluation.runner import fail_orphaned_runs, fail_run
from app.schemas.evaluations import EvaluationRun as EvaluationRunOut

from tests.fixtures.credentials import seed_placeholder_credentials
from tests.fixtures.database import (
    describe_connection_failure,
    migrate_to_head,
    truncate_all,
)

pytestmark = [pytest.mark.integration]

seed_placeholder_credentials()

REASON = "the process restarted while this run was in progress"


@pytest.fixture(scope="module")
def _schema() -> None:
    try:
        migrate_to_head()
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"{describe_connection_failure()}: {exc}")


@pytest.fixture(autouse=True)
async def _database(_schema: None) -> Any:
    await truncate_all()
    yield
    await truncate_all()
    from app.db.session import dispose_engine

    await dispose_engine()


async def _make_run(session: Any, status: str) -> uuid.UUID:
    from datetime import UTC, datetime

    from app.db.models import EvaluationRun

    run_id = uuid.uuid4()
    session.add(
        EvaluationRun(
            id=run_id,
            status=status,
            dataset_version="1.3.0",
            dataset_fingerprint="fp",
            config_snapshot={"eval_gate_recall_at_5": 0.8},
            started_at=datetime.now(UTC),
            question_count=32,
            # `metrics` and `thresholds` are NOT NULL, and `gate_outcome` may only
            # be `pass` or `fail`: a run that has not been judged reads as `fail`,
            # because it has not earned a pass.
            metrics={},
            thresholds={"eval_gate_recall_at_5": 0.8},
            gate_outcome="fail",
            gate_failures=[],
        )
    )
    await session.commit()
    return run_id


async def test_an_interrupted_run_becomes_failed_with_a_reason() -> None:
    from app.db.models import EvaluationRun
    from app.db.session import get_session_factory

    factory = get_session_factory()
    async with factory() as session:
        run_id = await _make_run(session, "running")

    async with factory() as session:
        assert await fail_orphaned_runs(session, reason=REASON) == 1

    async with factory() as session:
        run = await session.get(EvaluationRun, run_id)
        assert run is not None
        assert run.status == "failed"
        assert run.finished_at is not None, "a failed run must have an end time"
        assert run.error == REASON


async def test_a_queued_run_is_orphaned_too() -> None:
    """`queued` blocks a new run exactly as `running` does.

    A run is persisted as `queued` before its task starts, so a process that dies
    in that window leaves a row that `create_run` counts as active.
    """
    from app.db.models import EvaluationRun
    from app.db.session import get_session_factory

    factory = get_session_factory()
    async with factory() as session:
        run_id = await _make_run(session, "queued")

    async with factory() as session:
        assert await fail_orphaned_runs(session, reason=REASON) == 1

    async with factory() as session:
        run = await session.get(EvaluationRun, run_id)
        assert run is not None and run.status == "failed"


async def test_a_completed_run_is_never_relabelled() -> None:
    """Reconciliation must not touch a run that finished.

    `fail_run` returns a bool for exactly this: a late callback arriving after a
    successful run would otherwise overwrite `completed` with `failed`, which is a
    worse bug than the one being fixed.
    """
    from app.db.models import EvaluationRun
    from app.db.session import get_session_factory

    factory = get_session_factory()
    async with factory() as session:
        run_id = await _make_run(session, "completed")

    async with factory() as session:
        assert await fail_orphaned_runs(session, reason=REASON) == 0
        assert await fail_run(session, run_id, reason=REASON) is False

    async with factory() as session:
        run = await session.get(EvaluationRun, run_id)
        assert run is not None and run.status == "completed"


async def test_partial_results_are_kept() -> None:
    """The measurements that were taken are not measurements to discard.

    A run interrupted at question 24 of 32 recorded 24 rows. Tidyuing a status
    field by deleting real numbers would be the wrong trade.
    """
    from app.db.models import EvaluationQuestion, EvaluationResult
    from app.db.session import get_session_factory
    from sqlalchemy import func, select

    factory = get_session_factory()
    async with factory() as session:
        run_id = await _make_run(session, "running")
        # Committed separately: there is no ORM relationship between the models,
        # so flush order is insertion order and the result's foreign key would be
        # checked before its parent existed.
        session.add(
            EvaluationQuestion(
                id="simple-jira-filter-saved",
                question="How do I save a filter in Jira Cloud?",
                category="simple_lookup",
                difficulty="easy",
                is_unsupported=False,
                expected_product="jira",
                expected_category="filters",
                expected_heading_path=["Save your search as a filter"],
                expected_topics=["a filter can be saved for reuse"],
                tags=["jira"],
                dataset_version="1.3.0",
            )
        )
        await session.commit()
        session.add(
            EvaluationResult(
                run_id=run_id,
                question_id="simple-jira-filter-saved",
                refused=False,
                fabricated_fact_count=0,
                invalid_citation_count=0,
            )
        )
        await session.commit()

    async with factory() as session:
        await fail_orphaned_runs(session, reason=REASON)

    async with factory() as session:
        kept = (
            await session.execute(
                select(func.count(EvaluationResult.id)).where(EvaluationResult.run_id == run_id)
            )
        ).scalar_one()
        assert kept == 1


def test_the_response_carries_the_reason() -> None:
    """The exposure half: a failed run must not be a failed run with no explanation."""
    out = EvaluationRunOut(
        id=str(uuid.uuid4()),
        dataset_version="1.3.0",
        dataset_fingerprint="fp",
        status="failed",
        gate_outcome="fail",
        error=REASON,
    )

    assert out.error == REASON
    assert "error" in out.model_dump()

    # And absent on a completed run, rather than an empty string that reads like
    # something went wrong.
    completed = EvaluationRunOut(
        id=str(uuid.uuid4()),
        dataset_version="1.3.0",
        dataset_fingerprint="fp",
        status="completed",
        gate_outcome="pass",
    )
    assert completed.error is None
    assert completed.model_dump()["error"] is None
