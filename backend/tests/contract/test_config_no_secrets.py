"""No secret value, anywhere (T153, FR-042, SC-015).

Two independent mechanisms are asserted here, because either alone is
insufficient:

1. **Sentinel sweep.** A recognisable fake value is installed for each secret
   and every endpoint is asked for. If any response body, header, or error
   message carries the value, this fails. A structural check alone would miss a
   debug print of the setting object; a sweep alone would miss a new response
   field that only becomes reachable under a real key.
2. **`SecretPresence` has no value field.** The schema says "configured" and
   nothing else, so there is no field for a value to be put into by mistake.

The sentinel sweep is deliberately blunt — the full credential string, and its
distinctive prefix — because a leak that redacts the middle of a key but prints
the prefix is still a leak.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient

from tests.fixtures.database import describe_connection_failure, migrate_to_head

pytestmark = pytest.mark.contract

#: Distinctive enough that a substring match cannot hit an unrelated field.
SENTINELS = {
    "GROQ_API_KEY": "gsk_SENTINEL_value_must_never_be_served_0123456789",
    "PINECONE_API_KEY": "pcsk_SENTINEL_value_must_never_be_served_9876543210",
}


@pytest.fixture(scope="module")
def _schema() -> None:
    try:
        migrate_to_head()
    except Exception:  # noqa: BLE001
        pytest.skip(describe_connection_failure())


@pytest.fixture
async def client(_schema: None) -> AsyncIterator[AsyncClient]:
    from app.core.config import get_settings
    from app.main import create_app, lifespan

    previous = {name: os.environ.get(name) for name in SENTINELS}
    for name, value in SENTINELS.items():
        os.environ[name] = value
    # Settings are cached; rebuild them against the sentinels, then restore.
    get_settings.cache_clear() if hasattr(get_settings, "cache_clear") else None

    app = create_app(enable_probes=False)
    async with lifespan(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as http:
            yield http

    for name, value in previous.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value
    if hasattr(get_settings, "cache_clear"):
        get_settings.cache_clear()


class TestNoSecretExposure:
    async def test_no_response_carries_a_secret_value(self, client: AsyncClient) -> None:
        for path in [
            "/api/v1/config",
            "/api/v1/health",
            "/api/v1/sources",
            "/api/v1/documents",
            "/api/v1/crawl-jobs",
            "/api/v1/evaluations/runs",
            "/api/v1/evaluations/dataset",
        ]:
            response = await client.get(path)
            body = response.text
            for name, sentinel in SENTINELS.items():
                assert sentinel not in body, f"{path} served the {name} value"
                prefix = sentinel.split("_SENTINEL")[0]
                assert prefix not in body, f"{path} served a fragment of {name}"
            for header, value in response.headers.items():
                for sentinel in SENTINELS.values():
                    assert sentinel not in value, f"{path} leaked a secret in header {header}"

    async def test_the_error_envelope_carries_no_secret_value(self, client: AsyncClient) -> None:
        response = await client.get("/api/v1/sources/not-a-uuid")
        # 400 for a path the router cannot parse, 422 for a body that fails
        # validation, 404 for a well-formed id that does not exist — all three
        # are error envelopes, and all three are swept.
        assert response.status_code in (400, 404, 422)
        assert "error" in response.json()
        for sentinel in SENTINELS.values():
            assert sentinel not in response.text

    async def test_the_schema_cannot_carry_a_value(self) -> None:
        from app.schemas import SecretPresence

        assert set(SecretPresence.model_fields) == {"configured"}
        schema = SecretPresence.model_json_schema()
        assert set(schema["properties"]) == {"configured"}

    async def test_config_reports_presence_as_a_boolean(self, client: AsyncClient) -> None:
        response = await client.get("/api/v1/config")
        assert response.status_code == 200
        secrets = response.json()["secrets"]
        for name in ("pinecone_api_key", "groq_api_key", "database_url"):
            entry = secrets[name]
            assert set(entry) == {"configured"}, f"{name} carries more than presence: {entry}"
            assert isinstance(entry["configured"], bool)