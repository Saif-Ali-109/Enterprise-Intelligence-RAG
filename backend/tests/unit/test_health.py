"""`/health` and the probe registry.

T037, T038. The endpoint exists so a dependency outage is visible as *that
dependency's* outage, and the tests are mostly about the cases where a health
check is worse than none: a green result that is a lie, and a red result whose
reason is not actionable.
"""

from __future__ import annotations

import json

import pytest
from app.api.routes_health import (
    DATABASE,
    DEPENDENCY_ORDER,
    LANGUAGE_MODEL,
    VECTOR_STORE,
    register_probe,
    registered_probes,
)
from httpx import ASGITransport

from tests.fixtures.vendors import (
    FakePineconeClient,
    make_index_model,
)

pytestmark = pytest.mark.unit


# ============================================================================
# Registry
# ============================================================================


class TestProbeRegistry:
    def test_registration_and_reset(self) -> None:
        async def probe() -> str | None:
            return None

        register_probe(DATABASE, probe)
        assert DATABASE in registered_probes()

        from app.api.routes_health import reset_probes

        reset_probes()
        assert registered_probes() == {}

    def test_registered_probes_returns_a_copy(self) -> None:
        """A caller that could mutate the registry would be able to remove a
        dependency's probe and make `/health` stop reporting it."""

        async def probe() -> str | None:
            return None

        register_probe(DATABASE, probe)
        snapshot = registered_probes()
        snapshot.clear()
        assert DATABASE in registered_probes()

    def test_the_three_contract_dependencies_are_named_exactly(self) -> None:
        """The contract's `required` list names these three keys.

        A rename would still pass a test that only checked three dependencies
        existed.
        """
        assert DEPENDENCY_ORDER == ("database", "vector_store", "language_model")
        assert (DATABASE, VECTOR_STORE, LANGUAGE_MODEL) == DEPENDENCY_ORDER


# ============================================================================
# Probe behaviour
# ============================================================================


class TestProbeOutcomeHandling:
    async def test_a_raising_probe_becomes_a_status_not_a_500(self) -> None:
        """The endpoint is unavailable exactly when it is needed.

        A 500 from `/health` when the thing it monitors is broken is strictly
        worse than no endpoint: monitoring cannot distinguish "unhealthy" from
        "the probe itself is broken".
        """

        async def exploding() -> str | None:
            raise RuntimeError("vendor SDK blew up")

        register_probe(DATABASE, exploding)
        from app.api.routes_health import _run_probe

        report = await _run_probe(DATABASE)

        assert report.status.value == "unavailable"
        assert "RuntimeError" in (report.detail or "")
        # The exception's own message is not echoed: a vendor SDK exception
        # routinely carries the request that caused it, including a key.
        assert "blew up" not in (report.detail or "")

    async def test_a_probe_returning_a_reason_is_unavailable_with_that_reason(self) -> None:
        async def failing() -> str | None:
            return "Index is not ready: Initializing."

        register_probe(DATABASE, failing)
        from app.api.routes_health import _run_probe

        report = await _run_probe(DATABASE)

        assert report.status.value == "unavailable"
        assert report.detail == "Index is not ready: Initializing."

    async def test_latency_is_reported_as_an_integer(self) -> None:
        """The contract types `latency_ms` as an integer or null.

        A float with two decimals — which is what `round(x, 2)` produces —
        fails validation and turns a healthy dependency into an `INTERNAL_ERROR`.
        This is not hypothetical: it is what the first version of this code did.
        """

        async def slow() -> str | None:
            return None

        register_probe(DATABASE, slow)
        from app.api.routes_health import _run_probe

        report = await _run_probe(DATABASE)

        assert report.latency_ms is not None
        assert isinstance(report.latency_ms, int)
        assert json.dumps(report.model_dump(mode="json"))

    async def test_an_unregistered_probe_is_reported_not_defaulted_to_ok(self) -> None:
        """An omitted key fails the contract's `required` list; a defaulted `ok` is a lie."""
        from app.api.routes_health import _run_probe

        report = await _run_probe(VECTOR_STORE)

        assert report.status.value == "unavailable"
        assert "no probe is registered" in (report.detail or "").lower()


# ============================================================================
# The vector-store probe — the `input_type` check (R-001)
# ============================================================================


class TestVectorStoreProbe:
    async def test_a_correctly_configured_index_is_healthy(self) -> None:
        from app.api.probes import _vector_store_probe
        from app.retrieval import vector_store as module

        client = FakePineconeClient(
            index_model=make_index_model(read_input_type="query", write_input_type="passage")
        )
        module._store = module.PineconeVectorStore(client=client)  # noqa: SLF001

        assert await _vector_store_probe() is None

    async def test_a_symmetric_input_type_is_reported(self) -> None:
        """The whole reason this probe reads the schema.

        An index created with both sides as `passage` answers every query
        successfully and ranks them worse. Nothing raises, nothing logs, and the
        only observable symptom is that answers are slightly off — which is
        indistinguishable from the model being mediocre.
        """
        from app.api.probes import _vector_store_probe
        from app.retrieval import vector_store as module

        client = FakePineconeClient(
            index_model=make_index_model(read_input_type="passage", write_input_type="passage")
        )
        module._store = module.PineconeVectorStore(client=client)  # noqa: SLF001

        reason = await _vector_store_probe()

        assert reason is not None
        assert "input_type" in reason
        assert "degrades silently" in reason

    async def test_a_not_ready_index_reports_its_state(self) -> None:
        from app.api.probes import _vector_store_probe
        from app.retrieval import vector_store as module

        client = FakePineconeClient(
            index_model=make_index_model(ready=False, state="InitializationFailed")
        )
        module._store = module.PineconeVectorStore(client=client)  # noqa: SLF001

        reason = await _vector_store_probe()

        assert reason is not None
        assert "InitializationFailed" in reason

    async def test_a_wrong_embedding_model_is_reported_as_a_configuration_problem(self) -> None:
        """The model cannot be changed after creation, so the remedy is a re-index.

        Saying "unreachable" would send an operator to look at the network.
        """
        from app.api.probes import _vector_store_probe
        from app.retrieval import vector_store as module

        client = FakePineconeClient(index_model=make_index_model(model="multilingual-e5-large"))
        module._store = module.PineconeVectorStore(client=client)  # noqa: SLF001

        reason = await _vector_store_probe()

        assert reason is not None
        assert "multilingual-e5-large" in reason
        assert "recreated" in reason

    async def test_an_unreachable_service_names_the_exception_type_only(self) -> None:
        from app.api.probes import _vector_store_probe
        from app.retrieval import vector_store as module

        class Broken:
            @property
            def indexes(self) -> object:
                class Raising:
                    @staticmethod
                    def describe(name: str) -> None:
                        from pinecone import PineconeConnectionError

                        raise PineconeConnectionError("connection to host 10.0.0.5 refused")

                return Raising()

        module._store = module.PineconeVectorStore(client=Broken())  # noqa: SLF001

        reason = await _vector_store_probe()

        assert reason is not None
        assert "unreachable" in reason.lower()
        assert "10.0.0.5" not in reason


# ============================================================================
# The endpoint
# ============================================================================


async def _get(path: str, **kwargs: object):
    """Call the app in-process, running the real lifespan."""
    import httpx
    from app.main import create_app, lifespan

    app = create_app(enable_probes=False)
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        async with lifespan(app):
            return await client.get(path, **kwargs)  # type: ignore[arg-type]


class TestHealthEndpoint:
    async def test_all_three_dependencies_are_always_present(self) -> None:
        """Even with no probes registered. A missing key fails the contract's `required`."""
        response = await _get("/api/v1/health")

        assert response.status_code == 200
        assert set(response.json()["dependencies"]) == set(DEPENDENCY_ORDER)

    async def test_status_is_the_worst_dependency(self) -> None:
        async def ok() -> str | None:
            return None

        async def broken() -> str | None:
            return "unreachable"

        register_probe(DATABASE, ok)
        register_probe(VECTOR_STORE, broken)
        register_probe(LANGUAGE_MODEL, ok)

        import httpx
        from app.main import create_app

        app = create_app(enable_probes=False)
        transport = ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/api/v1/health")

        assert response.json()["status"] == "unavailable"
        # The rolled-up status must not hide which dependency is at fault.
        assert response.json()["dependencies"]["vector_store"]["status"] == "unavailable"
        assert response.json()["dependencies"]["database"]["status"] == "ok"

    async def test_all_ok_yields_ok(self) -> None:
        async def ok() -> str | None:
            return None

        for name in DEPENDENCY_ORDER:
            register_probe(name, ok)

        import httpx
        from app.main import create_app

        app = create_app(enable_probes=False)
        transport = ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/api/v1/health")

        assert response.json()["status"] == "ok"

    async def test_the_request_id_is_echoed(self) -> None:
        """So a bug report can quote a value visible in the browser's network tab."""
        response = await _get("/api/v1/health", headers={"X-Request-ID": "trace-abc-123"})

        assert response.headers["X-Request-Id"] == "trace-abc-123"

    async def test_a_hostile_request_id_is_replaced_not_rejected(self) -> None:
        """A newline in a header value is a log-injection vector.

        Rejecting the request would be worse than not honouring its correlation
        id, so it is silently replaced.
        """
        response = await _get("/api/v1/health", headers={"X-Request-ID": "a" * 5000})

        assert response.status_code == 200
        echoed = response.headers["X-Request-Id"]
        assert len(echoed) <= 64
        assert echoed != "a" * 5000

    async def test_the_version_is_reported(self) -> None:
        from app import __version__

        response = await _get("/api/v1/health")

        assert response.json()["version"] == __version__
