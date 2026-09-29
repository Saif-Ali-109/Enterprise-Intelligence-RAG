"""Structured logging with request correlation and secret redaction.

Constitution Principle X: operation is observable, and observability never
becomes a disclosure channel. Two things follow, and they are the reason this
is one module rather than a logging call scattered through the codebase.

**Every record carries a request id.** A question that took 7 seconds across
six pipeline stages produces six records, and without a shared key they are six
unrelated lines. `request_id` is the correlation key for the whole request and
matches `query_logs.id`, so an operator can join a log to an audit record
(FR-047, FR-048).

**Nothing secret is ever formatted.** The `SecretRedactionFilter` runs on every
record, including on records emitted by third-party libraries, and it works on
the *rendered message* rather than on the call arguments. That distinction
matters: a vendor SDK that formats a header into its own message before logging
it has already bypassed any argument-level filter. Filtering the final string is
the only place a value that arrived by any route can still be caught.

The filter is a backstop, not the mechanism. Secrets are `SecretStr` in
`config.py` and never have a readable accessor; this catches the case where a
credential arrives from outside the settings object — a header, a response body,
a response from a vendor — and is defence in depth rather than the plan.
"""

from __future__ import annotations

import json
import logging
import re
import sys
import time
import uuid
from contextvars import ContextVar, Token
from typing import Any, Final, cast

# ============================================================================
# Request correlation
# ============================================================================

# A ContextVar rather than a thread-local: the request id has to survive
# `await`, so a task spawned inside a request handler still logs with the right
# id. A threading.local would be empty in that task and the records would be
# unattributable — which is exactly the failure this is meant to prevent.
_request_id: ContextVar[str | None] = ContextVar("request_id", default=None)


def new_request_id() -> str:
    """Mint a correlation id. Matches the `query_logs.id` column type (UUIDv4)."""
    return str(uuid.uuid4())


def set_request_id(request_id: str) -> Token[str | None]:
    """Bind a request id to the current context."""
    return _request_id.set(request_id)


def reset_request_id(token: Token[str | None]) -> None:
    """Restore the previous binding. Always paired with `set_request_id`."""
    _request_id.reset(token)


def get_request_id() -> str | None:
    """The current request id, or `None` outside a request (startup, CLI)."""
    return _request_id.get()


# ============================================================================
# Secret redaction
# ============================================================================

# Pattern shapes, not values. Matching on the prefix means a truncated or
# partially-logged key is still caught, and no real credential appears in this
# file, in the repository, or in the filter's own output.
_SECRET_PATTERNS: Final[tuple[re.Pattern[str], ...]] = tuple(
    re.compile(pattern)
    for pattern in (
        # Groq
        r"gsk_[A-Za-z0-9]{20,}",
        # Pinecone
        r"pcsk_[A-Za-z0-9]{20,}",
        # OpenAI-style
        r"sk-[A-Za-z0-9]{20,}",
        # Google
        r"AIza[A-Za-z0-9_-]{20,}",
        # GitHub
        r"gh[pousr]_[A-Za-z0-9]{20,}",
        # Slack
        r"xox[abprs]-[A-Za-z0-9-]{10,}",
        # JWT — a signed token is a bearer credential
        r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}",
        # A connection string carrying inline credentials. The scheme and the
        # separator are enough; the password is matched as `[^@]`.
        r"\b[a-zA-Z][a-zA-Z0-9+.-]*://[^\s:/@]+:[^\s@]+@",
        # A bearer header, whatever the scheme name
        r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{16,}",
        # An Authorization or x-api-key header value, in any serialisation
        r"(?i)(authorization|x-api-key|api[-_]?key)([\"']?\s*[:=]\s*[\"']?)([A-Za-z0-9._~+/=-]{12,})",
    )
)

_REDACTED: Final[str] = "[REDACTED]"

# `connection_string` needs the password replaced but the host kept, because
# knowing WHICH database was unreachable is what makes a log useful. The
# generic pattern would redact the whole URL and lose that.
_CREDENTIAL_IN_URL: Final[re.Pattern[str]] = re.compile(
    r"(\b[a-zA-Z][a-zA-Z0-9+.-]*://[^\s:/@]+:)([^\s@]+)(@)"
)


def redact(text: str) -> str:
    """Remove anything shaped like a credential from a rendered string.

    Applied to log messages, exception text, and anything else that might be
    rendered to an operator. Exposed separately from the filter so the error
    handler can use the same function for response bodies — a stack trace is
    more likely to contain a URL than a log line is, and the two must not
    drift apart.
    """
    if not text:
        return text

    result = _CREDENTIAL_IN_URL.sub(rf"\1{_REDACTED}\3", text)
    for pattern in _SECRET_PATTERNS:
        result = pattern.sub(_REDACTED, result)
    return result


def _redact_value(value: Any, *, _depth: int = 0) -> Any:
    """Recursively scrub a structured log attribute.

    Walks strings, dicts, lists, and tuples. Anything else — a session, a
    response, a datetime — is left exactly as it is: descending into arbitrary
    objects means calling `str()` on them, which for a database driver can
    produce a query with a bound parameter in it, and a log call should not have
    that side effect.

    Bounded at four levels because a self-referential structure is otherwise an
    infinite walk, and a log call that hangs the process is a worse outcome than
    a log call with one unsanitised leaf.
    """
    if _depth > 4:
        return value

    if isinstance(value, str):
        return redact(value)

    if isinstance(value, dict):
        return {
            _redact_value(key, _depth=_depth + 1): _redact_value(item, _depth=_depth + 1)
            for key, item in value.items()
        }

    if isinstance(value, list):
        return [_redact_value(item, _depth=_depth + 1) for item in value]

    if isinstance(value, tuple):
        return tuple(_redact_value(item, _depth=_depth + 1) for item in value)

    return value


class SecretRedactionFilter(logging.Filter):
    """Scrub every record: the rendered message, the format arguments, and
    every attribute attached with `extra=`.

    Applied to the *root* logger, so it covers records emitted by `httpx`,
    `httpcore`, `uvicorn`, `sqlalchemy`, and the vendor SDKs — none of which
    know anything about this system's secret policy and any of which will
    cheerfully log a request header.

    The `extra=` walk is the part that is easy to get wrong, and getting it
    wrong is silent. A filter that scrubs only `record.msg` passes every test
    written against `log.info("msg %s", key)` and leaks every
    `log.info("msg", extra={"api_key": key})`. Both shapes are used in this
    codebase, so both are scrubbed.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        # The message, before %-formatting.
        if isinstance(record.msg, str):
            record.msg = redact(record.msg)

        # The arguments, because the message is only a template at this point. A
        # pre-formatted string has already been through `redact` as the message.
        if record.args:
            if isinstance(record.args, dict):
                record.args = _redact_value(record.args)
            elif isinstance(record.args, tuple):
                record.args = _redact_value(record.args)
            else:
                # `LogRecord.args` is a public attribute, so a caller can put
                # anything on it even though the standard library only ever sets
                # a tuple or a mapping. typeshed narrows it to those two, so the
                # assignment below needs a cast rather than a bare write — the
                # branch is kept because a filter that trusts a type annotation
                # on an attribute it does not own is a filter that can be
                # bypassed by whoever calls it.
                record.args = cast("tuple[object, ...]", redact(str(record.args)))

        # Every attribute the caller attached. `extra=` is both the idiomatic
        # way to add structured context and the idiomatic way a secret arrives,
        # so it is scrubbed on the way OUT rather than trusted on the way in: a
        # caller cannot bypass the filter by choosing a different log shape.
        for key in list(record.__dict__):
            if key in _LOG_RECORD_BUILTINS:
                continue
            record.__dict__[key] = _redact_value(record.__dict__[key])

        # Always True. A filter that drops a record because it contained a
        # secret loses the fact that something happened, which is worse than
        # losing the secret's value: the error is now invisible AND the record
        # is gone. Redacting keeps the line and loses only the value.
        return True


_LOG_RECORD_BUILTINS: Final[frozenset[str]] = frozenset(
    logging.LogRecord("", 0, "", 0, "", (), None).__dict__.keys()
) | frozenset({"message", "asctime", "taskName"})


# ============================================================================
# Formatter
# ============================================================================


class JsonFormatter(logging.Formatter):
    """One JSON object per line.

    JSON rather than a human-readable line because these logs are consumed by a
    tool, not read over a shoulder. `duration_ms` on a stage record is a number
    a dashboard can aggregate; "took 1.234s" is prose.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        # Bound per request by the middleware. Falls back to the value already
        # on the record, so a log emitted inside a request but from a library
        # that does not know about ContextVars is still attributed.
        request_id = get_request_id() or getattr(record, "request_id", None)
        if request_id:
            payload["request_id"] = request_id

        for key, value in record.__dict__.items():
            if key in _LOG_RECORD_BUILTINS or key in _RESERVED_PAYLOAD_KEYS:
                continue
            if key.startswith("_"):
                continue
            payload[key] = value

        if record.exc_info:
            # The exception TYPE and message only. A rendered traceback carries
            # local variable values and file paths, and goes to a log sink that
            # may be far less carefully access-controlled than the API.
            exc_type, exc_value, _ = record.exc_info
            payload["error"] = {
                "type": exc_type.__name__ if exc_type else "Unknown",
                "message": redact(str(exc_value)) if exc_value else "",
            }

        return json.dumps(payload, default=str, separators=(",", ":"))


_RESERVED_PAYLOAD_KEYS: Final[frozenset[str]] = frozenset(
    {
        "level",
        "logger",
        "message",
        "request_id",
        "name",
        "args",
        "exc_info",
        "exc_text",
        "stack_info",
        "pathname",
        "filename",
        "module",
        "lineno",
        "funcName",
        "created",
        "msecs",
        "relativeCreated",
        "thread",
        "threadName",
        "process",
        "processName",
        "taskName",
    }
)


# ============================================================================
# Configuration
# ============================================================================


def configure_logging(level: str = "INFO", *, force: bool = False) -> None:
    """Install the JSON formatter and the redaction filter on the root logger.

    Idempotent unless `force`. Handlers accumulate silently on the root logger
    if this is called more than once, and a duplicated line looks exactly like a
    request that was processed twice — a confusing bug to chase for a
    configuration mistake.
    """
    root = logging.getLogger()
    numeric_level = getattr(logging, level.upper(), logging.INFO)

    if root.handlers and not force:
        # Still apply the filter to handlers installed elsewhere, so an
        # embedding application cannot accidentally un-redact the logs.
        for handler in root.handlers:
            _install_filter(handler)
        return

    root.handlers.clear()
    root.setLevel(numeric_level)

    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(JsonFormatter())
    _install_filter(handler)

    root.addHandler(handler)

    # uvicorn installs its own colourised access-log formatter. It is replaced
    # rather than silenced, because an access log is a genuine observability
    # signal and removing it to make output tidier would be a regression.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.propagate = True

    # The vendor SDKs log at INFO and DEBUG with request detail. Left at WARNING
    # so a chatty HTTP client cannot bury the pipeline's own stage records,
    # which are the ones an operator is actually reading.
    for name in ("httpx", "httpcore", "urllib3", "pinecone", "groq", "sqlalchemy.engine"):
        logging.getLogger(name).setLevel(logging.WARNING)


def _install_filter(handler: logging.Handler) -> None:
    if not any(isinstance(existing, SecretRedactionFilter) for existing in handler.filters):
        handler.addFilter(SecretRedactionFilter())


def get_logger(name: str) -> logging.Logger:
    """A logger under the `app` namespace.

    Every application logger goes through here so there is one prefix to filter
    on and one place to look when something is too quiet.
    """
    return logging.getLogger(name if name.startswith("app") else f"app.{name}")


# ============================================================================
# Pipeline stage timing
# ============================================================================


class StageTimer:
    """Context manager recording a pipeline stage's duration.

    The pipeline is the product, and stage latency is how you find out which
    stage ate the 8-second budget (SC-017). Doing this with a decorator at each
    call site would scatter it; doing it here makes "log the stage" the default
    rather than something to remember.

        with StageTimer(self._log, "retrieve", **ctx):
            candidates = await self._retriever.retrieve(...)

    The record carries `duration_ms` and `stage`, plus whatever context the
    caller passes. It deliberately does NOT carry the stage's inputs or outputs:
    a retrieved chunk or a generated answer is third-party content going into a
    log sink, and FR-032's concern is packaging, not logging — but the same
    reasoning applies with more force to a sink that is usually less protected
    than the database.
    """

    __slots__ = ("_log", "_stage", "_context", "_start")

    def __init__(self, log: logging.Logger, stage: str, **context: Any) -> None:
        self._log = log
        self._stage = stage
        self._context = context
        self._start: float = 0.0

    def __enter__(self) -> StageTimer:
        self._start = time.perf_counter()
        return self

    def __exit__(self, exc_type: type[BaseException] | None, *_: object) -> None:
        duration_ms = round((time.perf_counter() - self._start) * 1000, 2)

        if exc_type is not None:
            # A failed stage is logged at ERROR with the exception TYPE. Not the
            # traceback: the handler for the request will decide whether the
            # detail is safe to show, and duplicating it here would risk
            # putting it somewhere it is not.
            self._log.error(
                "pipeline stage failed",
                extra={
                    "stage": self._stage,
                    "duration_ms": duration_ms,
                    "error_type": exc_type.__name__,
                    **self._context,
                },
            )
            return

        self._log.info(
            "pipeline stage complete",
            extra={"stage": self._stage, "duration_ms": duration_ms, **self._context},
        )


__all__ = [
    "JsonFormatter",
    "SecretRedactionFilter",
    "StageTimer",
    "configure_logging",
    "get_logger",
    "get_request_id",
    "new_request_id",
    "redact",
    "reset_request_id",
    "set_request_id",
]
