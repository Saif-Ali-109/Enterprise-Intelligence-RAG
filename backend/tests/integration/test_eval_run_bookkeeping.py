"""Every question in a run leaves its own audit row (the live-run defect).

**What went wrong, measured.** The first live evaluation run wrote 32
`evaluation_results` rows and then died with `MissingGreenlet`, while the log
carried a `duplicate key value violates unique constraint "query_logs_pkey"`
for one repeated request id. Two separate causes:

1. `run_chat` takes its request id from the ambient context (FR-047), and an
   asyncio task inherits the context of the request that spawned it. So every
   question in a background run reused the id of the POST that started it, and
   all but the first audit write collided on the primary key.
2. Reading `run.thresholds` after a commit touched an expired ORM attribute
   outside a greenlet context, which raised instead of returning the value.

**Why this test can afford a stub.** An empty store makes every question refuse
at the quality gate, before any provider call — so the whole run is
deterministic, fast, and offline. What is under test is the run's bookkeeping
around the pipeline, not the pipeline's answers (those belong to
`test_cited_answer.py` and to the evaluation run itself).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import func, select

from tests.fixtures.database import describe_connection_failure, migrate_to_head, truncate_all

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def _schema() -> None:
    try:
        migrate_to_head()
    except Exception:  # noqa: BLE001
        pytest.skip(describe_connection_failure())


@pytest.fixture(autouse=True)
async def _database(_schema: None) -> AsyncIterator[None]:
    await truncate_all()
    yield
    await truncate_all()
    from app.db.session import dispose_engine

    await dispose_engine()


class _EmptyStore:
    async def search(self, **_kwargs: Any) -> list[Any]:
        return []

    async def fetch(self, *_args: Any, **_kwargs: Any) -> list[Any]:
        return []


class _ClassifierOnlyProvider:
    """Classification only; an empty corpus refuses before generation.

    `classify` runs before retrieval, so it cannot be skipped — this stub
    answers it with zero confidence on every field, which suppresses the
    product filter (nothing to be wrong about) and lets the empty store refuse
    at the quality gate. `complete` is never reached and says so loudly if it
    ever is.
    """

    async def classify(self, **_kwargs: Any) -> dict[str, Any]:
        return {
            "detected_product": None,
            "detected_category": None,
            "intent": "factual",
            "entities": [],
            "confidence": {"product": 0.0, "category": 0.0, "intent": 0.0, "entities": 0.0},
        }

    async def complete(self, *_args: Any, **_kwargs: Any) -> Any:  # pragma: no cover
        raise AssertionError("generation must not be reached with an empty corpus")


async def _run_once() -> tuple[uuid.UUID, int]:
    from app.db.session import get_session_factory
    from app.evaluation.runner import create_run, execute_run

    factory = get_session_factory()
    async with factory() as session:
        run = await create_run(session)
        run_id = run.id
        question_count = run.question_count

    # The ambient request id a POST /evaluations/runs leaves behind. Binding it
    # here is what makes this test reproduce the production condition: without an
    # ambient id, `run_chat` mints a fresh one per question and the collision
    # cannot happen — which is exactly why the first version of this test passed
    # against code that was broken in production.
    from app.core.logging import new_request_id, reset_request_id, set_request_id

    token = set_request_id(new_request_id())
    try:
        await execute_run(run_id, store=_EmptyStore(), provider=_ClassifierOnlyProvider())
    finally:
        reset_request_id(token)
    return run_id, question_count


class TestRunBookkeeping:
    async def test_the_run_reaches_a_terminal_status_with_real_metrics(self) -> None:
        from app.db.models import EvaluationRun
        from app.db.session import get_session_factory

        run_id, _ = await _run_once()
        async with get_session_factory()() as session:
            finished = await session.get(EvaluationRun, run_id)
            assert finished is not None
            assert finished.status == "completed", "the run never reached a terminal status"
            assert finished.metrics, "a completed run with no metrics is a run that did not happen"
            assert finished.finished_at is not None

    async def test_each_question_gets_its_own_audit_row(self) -> None:
        from app.db.models import QueryLog
        from app.db.session import get_session_factory

        run_id, question_count = await _run_once()
        async with get_session_factory()() as session:
            total = int((await session.execute(select(func.count(QueryLog.id)))).scalar_one())
            distinct = int(
                (await session.execute(select(func.count(func.distinct(QueryLog.id))))).scalar_one()
            )

        assert total >= question_count, (
            f"{total} audit rows for {question_count} questions — a shared request id "
            f"collapses every question onto one row, and all but the first write fails"
        )
        assert distinct == total, "two questions were audited under one request id"

    async def test_a_refused_question_records_no_fabrication(self) -> None:
        from app.db.models import EvaluationResult
        from app.db.session import get_session_factory

        run_id, question_count = await _run_once()
        async with get_session_factory()() as session:
            rows = list(
                (
                    await session.execute(
                        select(EvaluationResult).where(EvaluationResult.run_id == run_id)
                    )
                ).scalars()
            )
        assert len(rows) == question_count
        assert all(row.refused for row in rows), [
            (row.question_id, row.error) for row in rows if not row.refused
        ][:5]
        assert all(row.fabricated_fact_count == 0 for row in rows)
        assert all(row.error is None for row in rows)
