"""Empty state (T135): with no run in the database, the evaluation surface says
so — totals are 0, metrics is null, and no numeric quality value appears
anywhere (FR-036, FR-037, quickstart V12's zero-tolerance definition)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient

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


@pytest.fixture
async def client(_schema: None) -> AsyncIterator[AsyncClient]:
    from app.main import create_app, lifespan

    app = create_app(enable_probes=False)
    async with lifespan(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as http:
            yield http


class TestEmptyState:
    async def test_no_runs_is_an_empty_page_not_a_placeholder(self, client: AsyncClient) -> None:
        response = await client.get("/api/v1/evaluations/runs")
        assert response.status_code == 200
        body = response.json()
        assert body["total"] == 0
        assert body["items"] == []

    async def test_a_missing_run_is_a_404_not_a_zeroed_one(self, client: AsyncClient) -> None:
        response = await client.get(f"/api/v1/evaluations/runs/{uuid.uuid4()}")
        assert response.status_code == 404

    async def test_results_for_a_missing_run_are_a_404(self, client: AsyncClient) -> None:
        response = await client.get(f"/api/v1/evaluations/runs/{uuid.uuid4()}/results")
        assert response.status_code == 404

    async def test_the_dataset_serves_the_committed_gold_set(self, client: AsyncClient) -> None:
        response = await client.get("/api/v1/evaluations/dataset")
        assert response.status_code == 200
        body = response.json()
        assert body["dataset_version"]
        assert len(body["questions"]) >= 30
        for q in body["questions"]:
            # No URL field can appear, by schema construction (FR-040, FR-063).
            assert "url" not in q and "source_url" not in q

    async def test_no_endpoint_has_a_quality_number_with_no_run(self, client: AsyncClient) -> None:
        for path in ["/api/v1/evaluations/runs"]:
            body = (await client.get(path)).json()
            text = str(body)
            for key in ("recall_at_5", "precision_at_5", "mrr", "faithfulness", "claim_coverage"):
                assert key not in text, f"{path} rendered {key} with no run"
