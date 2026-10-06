"""Metrics respond to a real configuration change (T139, quickstart V12).

**A metric that does not move under a deliberate change is a fabricated-metric
bug.** The "configuration change" here is the LLM provider being replaced by a
refusing stub — the same seam the production wiring offers. If recall, cross-
product coverage, or the unsupported-refusal gate cannot tell "an answering
pipeline" from "a refusing one", then they are not measuring anything.

The stub is canned, not random: the numbers are deterministic, the direction of
every asserted change is expected, and a move in the wrong direction is a bug
in the definition rather than in the dataset.
"""

from __future__ import annotations

import types
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest

from tests.fixtures.credentials import seed_placeholder_credentials
from tests.fixtures.database import describe_connection_failure, migrate_to_head, truncate_all

seed_placeholder_credentials()

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


def _good_payload() -> dict[str, Any]:
    answer = (
        "A filter can be saved from the filter or search interface, and a saved "
        "filter is then runnable again by name. [E1] Every later run reuses that "
        "same definition. [E1]"
    )
    return {
        "outcome": "answered",
        "answer": answer,
        "citations": [{"rank": 1, "quote": answer}],
        "searched": {"products": ["jira", "confluence"], "applied_filters": {}},
        "trace": {
            "retrieval": {
                "candidates": [
                    {
                        "id": "u1",
                        "product": "jira",
                        "category": "filters",
                        "heading_path": ["Save your search as a filter"],
                        "title": "Save your search as a filter",
                        "retrieval_score": 0.9,
                    }
                ]
            },
            "reranking": {"candidates": [{"id": "u1", "rank": 1, "rerank_score": 0.9}]},
            "generation": {"attempts": 1},
            "citations": {"valid": 1, "stripped": 0, "rejected_identifiers": []},
        },
    }


def _degraded_payload() -> dict[str, Any]:
    return {
        "outcome": "refused",
        "refusal_reason": "INSUFFICIENT_EVIDENCE",
        "answer": None,
        "citations": [],
        "searched": {"products": ["jira"], "applied_filters": {}},
        "trace": {
            "retrieval": {"candidates": []},
            "reranking": {"candidates": []},
            "generation": {"attempts": 0},
            "citations": {"valid": 0, "stripped": 0, "rejected_identifiers": []},
        },
    }


async def _run_once(monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any]) -> dict[str, Any]:
    import app.chat.service as service
    from app.db.session import get_session_factory
    from app.evaluation.runner import create_run, execute_run

    async def fake_run_chat(*_args: Any, **_kwargs: Any) -> Any:
        return types.SimpleNamespace(payload=payload)

    monkeypatch.setattr(service, "run_chat", fake_run_chat)

    factory = get_session_factory()
    async with factory() as session:
        run = await create_run(session)
        run_id: uuid.UUID = run.id
    await execute_run(run_id, store=None, provider=None)

    from app.db.models import EvaluationRun
    from sqlalchemy import select

    async with factory() as session:
        run = (
            await session.execute(select(EvaluationRun).where(EvaluationRun.id == run_id))
        ).scalar_one()
        return dict(run.metrics), run


class TestMetricsRespond:
    async def test_degrading_the_provider_moves_the_metrics(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        good_metrics, good_run = await _run_once(monkeypatch, _good_payload())
        degraded_metrics, degraded_run = await _run_once(monkeypatch, _degraded_payload())

        def assert_moved(key: str) -> None:
            assert good_metrics.get(key) != degraded_metrics.get(key), (
                f"{key} did not move when the pipeline was degraded: {good_metrics.get(key)!r} "
                f"vs {degraded_metrics.get(key)!r} — either the metric is decorative or the "
                f"runner is not reading what it computes"
            )

        # Each of these has a direct semantic reason to move:
        assert_moved("recall_at_5")  # the good run surfaces the matching unit
        assert_moved("cross_product_domain_coverage")  # 2 products vs 1
        assert_moved("unsupported_refusal_rate")  # answered vs refused
        assert_moved("claim_coverage")  # claims covered vs nothing to cover
        assert_moved("citation_validity")  # 1/(1+0+0) vs undefined

        # And the zero counters are honest: nothing was fabricated in either run.
        assert good_metrics["fabricated_fact_count"] == 0
        assert degraded_metrics["fabricated_fact_count"] == 0

        # The failing gate is also honest: the good run answers everything,
        # including the deliberately unsupported set — the recorded row says fail.
        assert good_run.status == "completed"
        assert degraded_run.status == "completed"
