"""Vendor health probes, registered only when their adapter exists.

T037 needs `/health` to report `vector_store` and `language_model` separately.
T029–T031 are the vendor adapters, and they are gated on a real API key, so
there is a window — the whole of the current phase — where the two probes have
no implementation to call.

That window is handled here rather than by writing a fake probe. A probe that
returns `ok` without contacting anything is worse than no probe: it turns the
health endpoint into decoration, and `/health` returning green while the vendor
is unreachable is the specific failure that makes a readiness check useless.

So the rule is: **a dependency with no adapter reports `unavailable` with a
detail saying why.** That is a truthful answer during this phase, and it becomes
`ok` the moment an adapter is registered — no edit to this file's callers.

`ping` is a cheap, non-mutating call. For Pinecone it describes the index
(supported in the installed SDK, see R-004 and T027); for Groq it lists models.
Neither writes anything, and neither costs money, so a monitoring system polling
every thirty seconds does not become a bill.
"""

from __future__ import annotations

from app.api.routes_health import LANGUAGE_MODEL, VECTOR_STORE, register_probe
from app.core.logging import get_logger

_log = get_logger("api.probes")

_registered: set[str] = set()


async def _vector_store_probe() -> str | None:
    """Confirm the configured index exists, is ready, and is configured correctly.

    Uses the adapter's own `describe()`, so the probe exercises the same client
    the retriever will use. A probe against a different client would answer for
    a different connection than the one that serves requests.

    Three checks, in increasing order of how quietly they would otherwise fail:

    1. **Reachability.** A network fault or a bad key.
    2. **Readiness.** An index that is still initializing cannot answer.
    3. **`input_type` configuration.** This is the one worth having. Pinecone
       documents that an `input_type` mismatch "quietly degrades" search quality
       while both calls still succeed, and `input_type` is fixed when the index
       is created. A health check that only tested reachability would report a
       misconfigured index as healthy forever, and the symptom — slightly worse
       answers — has no other cause anyone would look for. The service stores the
       setting on the index's semantic field, so it can be read back and compared
       rather than assumed (R-001).
    """
    # Imported lazily so a health check does not construct a vendor client it
    # may never need, and so an import failure is reportable rather than fatal.
    #
    # The fallback names the module that could not be imported instead of
    # asserting a build state. A "not built yet (T029)" message is only honest
    # while the absence is real; if the module is later renamed, that string
    # keeps reporting an unfinished task for finished work — which is how the
    # language-model probe came to report a permanently unavailable provider
    # for a full phase.
    try:
        from app.retrieval.vector_store import get_vector_store
    except ModuleNotFoundError as exc:
        return f"Vector service adapter could not be imported ({exc.name})."

    try:
        store = get_vector_store()
        info = await store.describe()
    except Exception as exc:  # noqa: BLE001
        # The exception TYPE is enough to act on and cannot carry a credential.
        return f"Vector service unreachable ({type(exc).__name__})."

    if not info.ready:
        return f"Index {info.name!r} is not ready: {info.state}."

    mismatch = info.input_type_mismatch()
    if mismatch is not None:
        return mismatch

    # A model other than the configured one means scores come from an embedding
    # this pipeline has not been evaluated against. Not an outage, so it is
    # reported as a configuration problem rather than an unavailability.
    configured = store.embed_model
    if info.embed_model and configured and info.embed_model != configured:
        return (
            f"Index is embedded with {info.embed_model!r} but this application is "
            f"configured for {configured!r}. The model cannot be changed after "
            f"creation; the index must be recreated."
        )

    return None


async def _language_model_probe() -> str | None:
    """Confirm the model provider answers and the configured model is available.

    Checks availability rather than issuing a generation: a generation call
    costs money and a monitoring poll should not. A 401 here is distinguished
    from a network fault in the message, because they have completely different
    remedies and the operator reading the dashboard needs to know which one they
    are looking at (R-006: an Enterprise-only model returns 401, not 404).
    """
    # Imported lazily so a settings-only command does not construct an HTTP
    # client it will never use. The module is `app.generation.provider`, which
    # is where T030 put it. An earlier version imported `app.generation.vendor`
    # — a name that does not exist — inside a `try/except ModuleNotFoundError`
    # whose fallback said "the adapter is not built yet". That import could
    # never succeed, so the probe reported a permanently unavailable language
    # model and pointed an operator at a task that was already complete. A
    # fallback that reports absence is only safe while the absence is real, so
    # this one names the import that failed rather than asserting a build state.
    from app.generation.provider import get_language_model

    try:
        client = get_language_model()
        available = await client.list_models()
    except Exception as exc:  # noqa: BLE001
        return f"Language model provider unreachable ({type(exc).__name__})."

    from app.core.config import get_settings

    settings = get_settings()
    ids = {model.id for model in available}

    missing = [
        name
        for name in (settings.groq_model, settings.groq_classification_model)
        if name not in ids
    ]
    if missing:
        # Not an outage — a configuration error against a working provider.
        return f"Configured model(s) not available to this key: {', '.join(missing)}."

    return None


def register_vendor_probes() -> None:
    """Register the two vendor probes. Idempotent."""
    for name, probe in (
        (VECTOR_STORE, _vector_store_probe),
        (LANGUAGE_MODEL, _language_model_probe),
    ):
        if name in _registered:
            continue
        register_probe(name, probe)
        _registered.add(name)
        _log.info("registered vendor probe", extra={"dependency": name})


__all__ = ["register_vendor_probes"]
