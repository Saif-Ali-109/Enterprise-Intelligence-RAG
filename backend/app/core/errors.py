"""The error envelope, and the mapping from exception to envelope.

Constitution Principle IX and FR-047: an error response tells a client what
happened, in a form a client can branch on, and tells them nothing else.

Three properties, and each one is a place where the obvious implementation is
wrong:

**The message is authored, not propagated.** `str(exc)` on a database driver
exception contains a SQL fragment and a file path. `str(exc)` on an HTTP
exception contains a URL, which may contain a credential. Every message in
`ErrorCode` is a literal written for display. An exception's own text is logged,
never returned.

**5xx says less than 4xx, not more.** An unexpected fault returns
`INTERNAL_ERROR` with `request_id` and no detail. The request id is the whole
payload because it is the only part that is both safe and useful — it is what
lets an operator find the real exception, which is logged, not returned.

**`VECTOR_SERVICE_UNAVAILABLE` is terminal.** It carries no partial results, no
cached answer, and no keyword-matched fallback. A degraded answer that looks
complete is worse than a visible failure, because the user cannot tell which one
they are looking at (FR-061, FR-062).
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ErrorCode(StrEnum):
    """The fourteen stable codes. Clients may branch on these across releases.

    The string values are the contract; the enum exists so a typo is an
    AttributeError at import time rather than a string that reaches a client
    and matches nothing.
    """

    VALIDATION_ERROR = "VALIDATION_ERROR"
    SSRF_BLOCKED = "SSRF_BLOCKED"
    INVALID_URL = "INVALID_URL"
    ROBOTS_DISALLOWED = "ROBOTS_DISALLOWED"
    UNAUTHORIZED = "UNAUTHORIZED"
    NOT_FOUND = "NOT_FOUND"
    CONFLICT = "CONFLICT"
    CRAWL_ALREADY_RUNNING = "CRAWL_ALREADY_RUNNING"
    DOCUMENT_DELETED = "DOCUMENT_DELETED"
    RATE_LIMITED = "RATE_LIMITED"
    PROVIDER_ERROR = "PROVIDER_ERROR"
    PROVIDER_RATE_LIMITED = "PROVIDER_RATE_LIMITED"
    VECTOR_SERVICE_UNAVAILABLE = "VECTOR_SERVICE_UNAVAILABLE"
    INTERNAL_ERROR = "INTERNAL_ERROR"


#: Code to HTTP status. One table, so a code cannot be returned under two
#: statuses by two different call sites — which would make a client's
#: `switch` incomplete in a way nobody notices until production.
STATUS_BY_CODE: dict[ErrorCode, int] = {
    ErrorCode.VALIDATION_ERROR: 400,
    ErrorCode.SSRF_BLOCKED: 400,
    ErrorCode.INVALID_URL: 400,
    ErrorCode.ROBOTS_DISALLOWED: 409,
    ErrorCode.UNAUTHORIZED: 401,
    ErrorCode.NOT_FOUND: 404,
    ErrorCode.CONFLICT: 409,
    ErrorCode.CRAWL_ALREADY_RUNNING: 409,
    ErrorCode.DOCUMENT_DELETED: 409,
    ErrorCode.RATE_LIMITED: 429,
    ErrorCode.PROVIDER_ERROR: 502,
    ErrorCode.PROVIDER_RATE_LIMITED: 503,
    ErrorCode.VECTOR_SERVICE_UNAVAILABLE: 503,
    ErrorCode.INTERNAL_ERROR: 500,
}

#: Authored, display-safe default message per code.
#:
#: Deliberately generic. A message that tried to be specific about *why* a
#: validation failed is a message that has to interpolate the input, and
#: interpolating the input is how free-form user text reaches a response body
#: unescaped. The specific reason goes in `details` as a field name, or in the
#: log, keyed by request id.
DEFAULT_MESSAGE: dict[ErrorCode, str] = {
    ErrorCode.VALIDATION_ERROR: "The request failed validation.",
    ErrorCode.SSRF_BLOCKED: "Target address is not an allowed public web address.",
    ErrorCode.INVALID_URL: "The URL is not a parseable absolute http or https address.",
    ErrorCode.ROBOTS_DISALLOWED: "The site's robots.txt directives disallow this path.",
    ErrorCode.UNAUTHORIZED: "A required service credential is absent or unusable.",
    ErrorCode.NOT_FOUND: "No such resource.",
    ErrorCode.CONFLICT: "The resource's current state forbids this operation.",
    ErrorCode.CRAWL_ALREADY_RUNNING: "A crawl for this source is already in progress.",
    ErrorCode.DOCUMENT_DELETED: "This document has been deleted and cannot be re-crawled.",
    ErrorCode.RATE_LIMITED: "Too many requests. Slow down and retry.",
    ErrorCode.PROVIDER_ERROR: "The language model provider failed or returned unusable output.",
    ErrorCode.PROVIDER_RATE_LIMITED: "The language model provider is throttling requests.",
    ErrorCode.VECTOR_SERVICE_UNAVAILABLE: "The vector service is unreachable. No answer was produced.",
    ErrorCode.INTERNAL_ERROR: "An unexpected error occurred.",
}

#: Codes that must never carry a `details` payload.
#:
#: An internal error's details are, by definition, the thing that was
#: suppressed. `INTERNAL_ERROR` is in this set for the same reason
#: `VECTOR_SERVICE_UNAVAILABLE` carries no partial results: a "helpful" detail
#: field on a 500 is a disclosure channel wearing a diagnostics costume.
_DETAIL_FORBIDDEN: frozenset[ErrorCode] = frozenset(
    {ErrorCode.INTERNAL_ERROR, ErrorCode.VECTOR_SERVICE_UNAVAILABLE}
)


class ErrorBody(BaseModel):
    """The inner object. Closed, so a new field cannot appear by accident."""

    model_config = ConfigDict(extra="forbid")

    code: ErrorCode = Field(description="Stable, machine-readable error code.")
    message: str = Field(description="Safe for display. Never a stack trace or a credential.")
    request_id: str = Field(description="Correlation id. Matches query_logs.id for a question.")
    details: dict[str, Any] | None = Field(
        default=None,
        description="Optional. Field names and enumerated values only, never free-form input.",
    )


class ErrorResponse(BaseModel):
    """The envelope every non-2xx response uses.

    No endpoint returns a bare `{"detail": ...}`. FastAPI's default shape is
    replaced globally in `main.py` precisely because two error formats on one
    API means every client writes two paths.
    """

    model_config = ConfigDict(extra="forbid")

    error: ErrorBody


class AppError(Exception):
    """Base class for every error this application raises deliberately.

    Carries the code, the authored message, and an optional details mapping. A
    subclass of `Exception` rather than of `HTTPException` so the domain layers
    do not import FastAPI: the crawler refusing an address is a fact about the
    crawl, not an HTTP response, and the same refusal has to hold when it is
    reached from a CLI or a test.
    """

    code: ErrorCode = ErrorCode.INTERNAL_ERROR

    def __init__(
        self,
        message: str | None = None,
        *,
        details: dict[str, Any] | None = None,
        request_id: str | None = None,
    ) -> None:
        self.message = message or DEFAULT_MESSAGE[self.code]
        # Details are dropped for the codes that must not carry them, rather
        # than being sanitised. Sanitising implies the content was close
        # enough to keep, and the point of the set is that it is not.
        self.details = None if self.code in _DETAIL_FORBIDDEN else details
        self.request_id = request_id
        super().__init__(self.message)

    @property
    def status_code(self) -> int:
        return STATUS_BY_CODE[self.code]

    def to_response(self, request_id: str) -> ErrorResponse:
        """Render the envelope, binding the request id from the request scope."""
        return ErrorResponse(
            error=ErrorBody(
                code=self.code,
                message=self.message,
                request_id=self.request_id or request_id,
                details=self.details,
            )
        )


# ---------------------------------------------------------------------------
# Concrete errors
# ---------------------------------------------------------------------------


class ValidationFailed(AppError):
    """Input failed validation or exceeded a bound (FR-043)."""

    code = ErrorCode.VALIDATION_ERROR


class SsrfBlocked(AppError):
    """Target is not an allowed public web address; refused before connecting.

    The message here is authored rather than interpolated with the offending
    host. Naming the host in a response confirms to whoever probed the endpoint
    exactly which internal addresses are in use, which is the information the
    guard exists to withhold. The host goes in the log, keyed by request id.
    """

    code = ErrorCode.SSRF_BLOCKED

    def __init__(self, *, reason: str, request_id: str | None = None) -> None:
        # `reason` is an enumerated value from the guard, e.g. "loopback",
        # "private_range", "link_local", "cloud_metadata", "non_web_scheme",
        # "not_allowlisted". Safe to expose: it describes the CATEGORY of
        # refusal, not the address that triggered it.
        super().__init__(details={"reason": reason}, request_id=request_id)


class InvalidUrl(AppError):
    """Not a parseable absolute http/https URL (FR-044)."""

    code = ErrorCode.INVALID_URL


class RobotsDisallowed(AppError):
    """The site's own directives disallow the path (FR-028)."""

    code = ErrorCode.ROBOTS_DISALLOWED


class CredentialMissing(AppError):
    """A required service credential is absent or unusable (FR-042)."""

    code = ErrorCode.UNAUTHORIZED


class ResourceNotFound(AppError):
    """No such resource."""

    code = ErrorCode.NOT_FOUND


class StateConflict(AppError):
    """The resource's state forbids the operation."""

    code = ErrorCode.CONFLICT


class CrawlAlreadyRunning(AppError):
    """A crawl for this source is in progress. The message names the job.

    The job id goes in `details`, not interpolated into the message: `details`
    is documented as carrying field names and enumerated values, and a UUID is
    one. The message stays the authored default so a client can match on it
    without parsing prose (FR-055).
    """

    code = ErrorCode.CRAWL_ALREADY_RUNNING

    def __init__(self, *, job_id: str, request_id: str | None = None) -> None:
        super().__init__(details={"running_job_id": job_id}, request_id=request_id)


class DocumentDeleted(AppError):
    """Tombstoned; an in-flight crawl cannot resurrect it (FR-056)."""

    code = ErrorCode.DOCUMENT_DELETED


class RateLimited(AppError):
    """Inbound limit exceeded. The handler attaches `Retry-After` (FR-045)."""

    code = ErrorCode.RATE_LIMITED

    def __init__(self, *, retry_after: int, request_id: str | None = None) -> None:
        self.retry_after = retry_after
        super().__init__(request_id=request_id)


class ProviderError(AppError):
    """The language model provider failed, or returned unusable output."""

    code = ErrorCode.PROVIDER_ERROR


class ProviderRateLimited(AppError):
    """Upstream throttling. `Retry-After` is attached when the provider gave one."""

    code = ErrorCode.PROVIDER_RATE_LIMITED

    def __init__(self, *, retry_after: int | None = None, request_id: str | None = None) -> None:
        self.retry_after = retry_after
        super().__init__(request_id=request_id)


class VectorServiceUnavailable(AppError):
    """The vector service is unreachable. Terminal by design (FR-061, FR-062).

    No fallback path is offered, and none should be added. A keyword search
    would answer the question, would be visibly worse, and would be
    indistinguishable from a good answer at a glance — which is precisely the
    failure mode a grounding system is supposed to make impossible.
    """

    code = ErrorCode.VECTOR_SERVICE_UNAVAILABLE


class InternalError(AppError):
    """Unexpected fault. Detail withheld; `request_id` returned (FR-047).

    Constructed by the exception handler with no arguments beyond the request
    id, so the original exception cannot leak into it by accident.
    """

    code = ErrorCode.INTERNAL_ERROR

    def __init__(self, request_id: str | None = None) -> None:
        super().__init__(request_id=request_id)


__all__ = [
    "AppError",
    "CrawlAlreadyRunning",
    "CredentialMissing",
    "DEFAULT_MESSAGE",
    "DocumentDeleted",
    "ErrorBody",
    "ErrorCode",
    "ErrorResponse",
    "InternalError",
    "InvalidUrl",
    "ProviderError",
    "ProviderRateLimited",
    "RateLimited",
    "ResourceNotFound",
    "RobotsDisallowed",
    "STATUS_BY_CODE",
    "SsrfBlocked",
    "StateConflict",
    "ValidationFailed",
    "VectorServiceUnavailable",
]
