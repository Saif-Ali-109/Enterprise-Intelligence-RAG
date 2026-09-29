"""The application factory, the middleware stack, and the error handlers.

T033. Three jobs, and the order they are installed in matters more than any of
them individually.

**1. Error handlers replace FastAPI's defaults, globally.**
FastAPI's default shapes a validation failure as `{"detail": [...]}` and an
unhandled exception as a bare 500 with no body. The contract says every non-2xx
response from every endpoint uses the `{"error": {...}}` envelope (FR-047). Two
error formats on one API means every client writes two paths and the second one
is the one nobody tests, so the defaults are replaced at the app level rather
than handled per route.

**2. The request-id middleware runs before anything that can fail.**
It mints or adopts an id, binds it to the `ContextVar` the logging module reads,
and echoes it on the response. Because the handlers read the id from the same
context, the `request_id` in an error envelope is the same value as the
`request_id` in the log records for that request and the same value as
`query_logs.id` — one key across all three, so a bug report needs no translation.

**3. The lifespan owns the resources.**
The database engine, the outbound HTTP client, and the vendor probes are opened
on startup and closed on shutdown. An engine left open exhausts PostgreSQL's
connection slots across a redeploy cycle; a client left open leaks a connection
pool. `dispose_engine()` in the shutdown is not tidiness, it is correctness.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from types import ModuleType
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import Response

from app import __version__
from app.api import routes_config, routes_health
from app.core.config import get_settings
from app.core.errors import (
    STATUS_BY_CODE,
    AppError,
    ErrorBody,
    ErrorCode,
    ErrorResponse,
    InternalError,
    RateLimited,
)
from app.core.logging import (
    configure_logging,
    get_logger,
    new_request_id,
    reset_request_id,
    set_request_id,
)
from app.core.ratelimit import client_key

_log = get_logger("main")

#: Inbound request header a client may supply to correlate its own logs. Absent
#: in almost all traffic, which is why a generated id is the normal case.
REQUEST_ID_HEADER: str = "X-Request-ID"

#: Set on every response, so a client that saw an error in a browser's network
#: tab can quote a value it can see rather than one it must ask the server for.
RESPONSE_REQUEST_ID_HEADER: str = "X-Request-ID"

_next_id: Callable[[], str] = new_request_id


# ============================================================================
# Middleware
# ============================================================================


async def request_id_middleware(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """Bind a request id for the duration of the request.

    An inbound id is adopted rather than discarded, so a request that crossed a
    load balancer or arrived from another service keeps its correlation key. It
    is validated first: the header is client-controlled, and an unvalidated id
    reaches a log sink and a database column, where a 4KB string or a newline is
    a problem. Anything that is not a short opaque token is replaced rather than
    rejected, because rejecting a request over its correlation id would be a
    worse outcome than not honouring it.
    """
    supplied = request.headers.get(REQUEST_ID_HEADER, "")
    request_id = supplied if _is_plausible_request_id(supplied) else _next_id()

    token = set_request_id(request_id)
    request.state.request_id = request_id
    started = time.perf_counter()

    try:
        response = await call_next(request)
    except Exception:
        # Re-raised rather than handled: the exception handlers registered below
        # produce the envelope, and a middleware that caught it would have to
        # duplicate that logic in a second place.
        raise
    finally:
        reset_request_id(token)

    response.headers[RESPONSE_REQUEST_ID_HEADER] = request_id
    response.headers["X-Response-Time-Ms"] = str(round((time.perf_counter() - started) * 1000, 2))

    # An access log record per request. The body is not logged — it holds the
    # question, which is the one thing in this system a user would not expect to
    # find in a log aggregator retained for 90 days by a third party.
    _log.info(
        "request complete",
        extra={
            "method": request.method,
            "path": request.url.path,
            "status_code": response.status_code,
            "duration_ms": round((time.perf_counter() - started) * 1000, 2),
            "client": client_key(request),
        },
    )

    return response


def _is_plausible_request_id(value: str) -> bool:
    """Whether an inbound `X-Request-ID` is safe to adopt.

    A UUID, or anything else short and printable. The length bound is the
    substantive check: the value is written to a log line and to a
    `TIMESTAMPTZ`-keyed audit table, and an unbounded client-supplied string
    going into both is a log-injection and storage-amplification vector.
    """
    if not value or len(value) > 64:
        return False
    if any(char in value for char in "\r\n\x00"):
        return False
    return all(char.isalnum() or char in "-_." for char in value)


# ============================================================================
# Error handlers
# ============================================================================


def _envelope(
    request: Request,
    code: ErrorCode,
    message: str | None = None,
    details: dict[str, Any] | None = None,
) -> JSONResponse:
    """Build the contract's error envelope for a request."""
    from app.core.errors import DEFAULT_MESSAGE

    request_id = getattr(request.state, "request_id", None) or str(uuid.uuid4())
    response = ErrorResponse(
        error=ErrorBody(
            code=code,
            message=message or DEFAULT_MESSAGE[code],
            request_id=request_id,
            details=details,
        )
    )
    return JSONResponse(status_code=STATUS_BY_CODE[code], content=response.model_dump(mode="json"))


async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    """Render a deliberate application error.

    `RateLimited` gets a `Retry-After` header, because the code is only useful to
    a client that knows when to try again, and a 429 without it invites an
    immediate retry that is also refused.
    """
    if isinstance(exc, RateLimited):
        response = _envelope(request, exc.code, details=exc.details)
        response.headers["Retry-After"] = str(exc.retry_after)
        return response

    if (
        isinstance(exc, AppError)
        and exc.request_id is None
        and getattr(request.state, "request_id", None)
    ):
        # Bind the request's id so the envelope and the log share it even when
        # the error was raised below the middleware.
        exc.request_id = request.state.request_id

    return _envelope(request, exc.code, message=exc.message, details=exc.details)


async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """Map a Pydantic/FastAPI validation failure to the envelope.

    The offending input is **not** echoed. FastAPI's default response includes
    the input value in each error entry, and the contract's `details` is
    documented as field names and enumerated values only — a 200KB body echoed
    into an error response is a denial-of-service with a JSON content type, and
    free-form text unescaped into a response is the other half of the problem.
    Only the field names and the failure kind are reported.
    """
    fields = sorted({str(error.get("loc", ("?",))[0]) for error in exc.errors()})
    _log.debug(
        "request validation failed", extra={"fields": fields, "error_count": len(exc.errors())}
    )
    return _envelope(request, ErrorCode.VALIDATION_ERROR, details={"fields": fields})


async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    """Give a Starlette-raised HTTPException the same envelope.

    Reaching here means a 404 from routing, a 405 from a method mismatch, or a
    500 from an inner handler. All three must be enveloped: an un-enveloped 404
    is the shape a client sees for a mistyped URL, which is exactly the response
    nobody documents.

    **Starlette's own `detail` is never propagated.** It is a bare string by
    convention, not by contract, and it comes from whatever raised — a
    dependency, a mount, a future middleware. The message here is always one this
    module or `errors.py` authored.

    `405` maps to `NOT_FOUND` and is reported as a 404. The contract's status
    table has no `405`, and the routers expose only the methods the contract
    defines, so a 405 means the client asked for a method that does not exist
    on this path — which is a not-found, not a validation failure of a body that
    was never read.
    """
    code = {404: ErrorCode.NOT_FOUND, 405: ErrorCode.NOT_FOUND}.get(
        exc.status_code,
        ErrorCode.INTERNAL_ERROR if exc.status_code >= 500 else ErrorCode.VALIDATION_ERROR,
    )
    return _envelope(request, code)


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """The last line: `INTERNAL_ERROR`, the request id, and nothing else.

    The exception is logged in full and returned as a request id. The two are
    deliberately asymmetric: an operator has the exception and a client has the
    id, and the id is what joins them. Returning anything more specific than
    this is a disclosure channel — a stack trace names internal paths, a driver
    message names the schema, and both end up in a browser or a support ticket.
    """
    request_id = getattr(request.state, "request_id", None) or str(uuid.uuid4())
    _log.exception(
        "unhandled exception",
        extra={"path": request.url.path, "method": request.method, "request_id": request_id},
    )
    return _envelope(request, ErrorCode.INTERNAL_ERROR, message=InternalError().message)


# ============================================================================
# Lifespan
# ============================================================================


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Open resources on startup, close them on shutdown.

    Startup validates the configuration first and refuses to serve if it is
    inconsistent. A process that starts with a retrieval budget of 12 -> 5 -> 3-6
    has already been caught by the settings validator; this is the second line
    that turns any *remaining* misconfiguration into a startup failure rather
    than a wrong answer at query time.
    """
    settings = get_settings()

    configure_logging(settings.log_level)
    _log.info(
        "starting",
        extra={
            "version": __version__,
            "index": settings.pinecone_index_name,
            "model": settings.groq_model,
            "embed_model": settings.pinecone_embed_model,
            "retrieval_budget": [
                settings.retrieval_candidate_pool,
                settings.retrieval_rerank_top_n,
                [settings.evidence_min_units, settings.evidence_max_units],
            ],
            "rate_limits": {
                "general": settings.rate_limit_general_per_minute,
                "ask": settings.rate_limit_ask_per_minute,
            },
        },
    )

    from app.ingestion.fetcher import SharedClient

    client = SharedClient()
    app.state.http_client = await client.__aenter__()

    # The database probe is the one that needs no vendor adapter, so it is
    # registered here and the vendor probes are registered by `create_app`
    # once the adapters are wired.
    routes_health.register_probe(routes_health.DATABASE, routes_health.database_probe)

    try:
        yield
    finally:
        _log.info("shutting down")

        from app.db.session import dispose_engine

        await client.__aexit__(None, None, None)
        await dispose_engine()
        routes_health.reset_probes()

        # Probes may hold a reference to the client that has just been closed.
        # Clearing them means a stale probe cannot be registered again by a
        # test or a reload.


# ============================================================================
# Factory
# ============================================================================


def create_app(*, enable_probes: bool = True) -> FastAPI:
    """Build the application.

    A factory rather than a module-level `app` so the test suite can build
    several independent applications, and so the vendor probes can be registered
    by a caller that has adapters to contribute. `enable_probes=False` gives a
    test an app whose `/health` reports every dependency as unprobed, which is
    the honest state for a test that is not testing health.
    """
    settings = get_settings()

    app = FastAPI(
        title="Enterprise Knowledge Intelligence RAG — Backend API",
        version=__version__,
        summary=(
            "Evidence-grounded question answering over publicly accessible documentation. "
            "An independent technical demonstration; not affiliated with, sponsored by, or "
            "endorsed by any documentation publisher."
        ),
        lifespan=lifespan,
        # The contract file is the published interface. Serving FastAPI's own
        # generated document instead would let the two drift, and a client
        # generated from the wrong one fails in a way nobody can diagnose from
        # the response.
        docs_url=f"{settings.api_prefix}/docs",
        openapi_url=f"{settings.api_prefix}/openapi.json",
        redoc_url=None,
    )

    # Order: middleware outermost, so the request id exists before any handler
    # or error path that might want to report it.
    app.middleware("http")(request_id_middleware)

    app.add_exception_handler(AppError, app_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(RequestValidationError, validation_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)  # type: ignore[arg-type]
    app.add_exception_handler(Exception, unhandled_exception_handler)

    _install_cors(app, settings.cors_origins)
    _install_routers(app, settings.api_prefix)

    if enable_probes:
        from app.api.probes import register_vendor_probes

        register_vendor_probes()

    return app


def _install_cors(app: FastAPI, origins: list[str]) -> None:
    """Permit exactly the configured origins, with no wildcard.

    `allow_credentials` is off, because this API has no cookie session and no
    user identity — there is nothing for a credentialed cross-origin request to
    add. Leaving it on is what turns a misconfigured origin into a data
    disclosure, and there is no reason to accept that risk for a single-operator
    local tool.
    """
    from fastapi.middleware.cors import CORSMiddleware

    if not origins:
        # No origins configured means the API is not reachable from a browser
        # at all. Not an error: the CLI and the container-to-container path both
        # work, and a default of `*` would silently make it reachable from one.
        _log.info("cors: no origins configured; browser access is disabled")
        return

    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", REQUEST_ID_HEADER, "Accept"],
        expose_headers=[RESPONSE_REQUEST_ID_HEADER, "X-Response-Time-Ms", "Retry-After"],
        max_age=600,
    )


def _install_routers(app: FastAPI, prefix: str) -> None:
    """Mount the routers that exist.

    T033's instruction is to "mount the routers that exist so far and mount all
    of them by the end of Phase 10", so this list grows as the stories land
    rather than being declared up front against modules that do not exist yet.
    Mounting a router that does not exist is an ImportError at startup, which
    would make the application unbootable for every phase before Phase 10 — the
    opposite of what a phased build is for.
    """
    app.include_router(routes_health.router, prefix=prefix)
    app.include_router(routes_config.router, prefix=prefix)

    # Placeholders for the story routers, mounted conditionally so a phase can
    # be run before the story that owns them is implemented. Each import is
    # guarded so a missing module means "not built yet", not "crash".
    for module_name, router_name in (
        ("app.api.routes_sources", "router"),
        ("app.api.routes_documents", "router"),
        ("app.api.routes_crawl_jobs", "router"),
        ("app.api.routes_chat", "router"),
        ("app.api.routes_evaluations", "router"),
    ):
        module = _try_import(module_name)
        if module is not None and hasattr(module, router_name):
            app.include_router(getattr(module, router_name), prefix=prefix)


def _try_import(module_name: str) -> ModuleType | None:
    """Import a module, returning `None` when it does not exist yet.

    Scoped to the requested module's own absence. A `ModuleNotFoundError` for
    anything *inside* it — a dependency that has not been written — propagates,
    because that is a broken module rather than an unbuilt phase, and silently
    returning `None` would mount nothing and report a healthy start.
    """
    import importlib

    try:
        return importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        if exc.name == module_name:
            return None
        raise


#: The ASGI application. Uvicorn's `--factory` flag is not needed; this is the
#: conventional module-level instance and it is what `uvicorn app.main:app`
#: expects.
app = create_app()


__all__ = [
    "REQUEST_ID_HEADER",
    "app",
    "app_error_handler",
    "create_app",
    "lifespan",
    "request_id_middleware",
    "unhandled_exception_handler",
    "validation_error_handler",
]
