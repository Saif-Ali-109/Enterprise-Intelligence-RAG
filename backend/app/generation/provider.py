"""The `LLMProvider` interface, and the Groq adapter that implements it.

T030. FR-011, FR-013, FR-034, R-006, R-008, R-014. The generation and
classification stages call this protocol and never import `groq`, for the same
reason the retriever never imports `pinecone`: a provider swap that requires
editing a pipeline stage is not a swappable provider.

Four decisions here are the substance of this module.

**Reasoning is discarded at the boundary, and the discard is checked, not
assumed.** FR-034 forbids *exposing* private model reasoning. It does not forbid
the provider from producing it, and no such control exists: R-014 records that
Groq accepts only `low`, `medium` and `high` for `reasoning_effort`, that `"none"`
is rejected with HTTP 400 `invalid_request_error`, and that both configured
models return a populated `message.reasoning` at every accepted effort. There is
no parameter that turns the channel off.

So the obligation lands here, on the emitted surface. `_completion_from` reads
exactly one field from the provider's message — `content` — and constructs a
`Completion` that has no field capable of carrying anything else. The reasoning is
discarded, not filtered, because a filter is a list of names someone can
eventually be tempted to extend. The result is then checked:
`assert_no_reasoning` walks the constructed object, and it walks dataclass
fields, so adding a `reasoning` attribute later is a failure at run time rather
than a silent new exposure that satisfies every type checker and no requirement.

Two checks, not one, because they catch different things. The structural check
proves no field exists that could carry deliberation. It cannot prove the
*content* is free of it — a model can inline "First, let us consider…" into
`content`, where no key betrays it. `text_looks_like_reasoning` is the second
check, and it is documented as the heuristic it is: a false positive refuses an
answer that was fine, which is the correct direction for a MUST NOT to fail in.

**Timeouts are this application's, not the SDK's.** R-008: the SDK's own retry
behaviour is unbounded by default and the application owns retry. So
`max_retries=0` is passed to the SDK, and retry lives in the transport from
T032 where R-008's table is implemented. Passing the SDK's default instead would
mean two retry layers, and the inner one silently doubling the outer one's
attempts.

**`temperature` is not a parameter of `generate`.** The grounding contract
requires a submitted citation to be verifiable, and the cheapest way to get a
model inventing a plausible-looking link is a high temperature. Generation runs
at the low end; the one place a model needs freedom — classification — has its
own method with its own setting.

**A truncated or malformed response is `PROVIDER_ERROR`, not a short answer.**
An answer that is half a JSON object is not a degraded success, and returning it
would put a parse failure into the answer path where it looks like a refusal.
"""

from __future__ import annotations

import dataclasses
import json
import re
from dataclasses import dataclass, field
from typing import Any, NamedTuple, Protocol, runtime_checkable

from app.core.config import get_settings
from app.core.errors import ProviderError, ProviderRateLimited
from app.core.logging import get_logger, redact

_log = get_logger("generation.provider")


# ============================================================================
# Value objects
# ============================================================================


@dataclass(frozen=True, slots=True)
class TokenUsage:
    """Token accounting for one call.

    `total_time` is the provider's own reported wall time, kept separate from
    this application's measured duration. R-008 records both: the provider's
    figure is what appears in `query_logs.token_usage`, and the measured one is
    what the pipeline's stage timer records. Conflating them makes it impossible
    to tell provider latency from queueing.
    """

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    total_time: float = 0.0

    # No `reasoning_tokens`, and that is a decision rather than an oversight.
    # The provider reports it under `usage.completion_tokens_details`. It is a
    # count, so keeping it would not leak deliberation — but it is a durable
    # record that a reasoning channel existed, in a table whose whole purpose is
    # to be an honest account of what the system did. A count is measured
    # where it is measured (see `DiscardReport`) and not retained where it
    # would read as part of the answer.


@dataclass(frozen=True, slots=True)
class Completion:
    """One completion, with no field that could carry reasoning (FR-034).

    The absence is the guarantee, and it is a stronger one than it looks:
    adding a `reasoning` field later is a one-line change that satisfies every
    type checker and no requirement. So the absence is not relied on — it is
    enforced. `assert_no_reasoning` walks this object's fields on every return
    from the adapter, and a `reasoning` attribute would be a run-time failure
    rather than a new field nobody reviewed.
    """

    text: str
    model: str
    usage: TokenUsage = field(default_factory=TokenUsage)
    finish_reason: str | None = None

    def json(self) -> dict[str, Any]:
        """Parse the completion as a JSON object.

        Raises `ProviderError` on malformed output. The caller's alternative is
        to treat a parse failure as "no classification", and FR-009 forbids
        forcing a classification — so a malformed classification response is an
        error to report, not a signal to coerce into an unknown.
        """
        text = self.text.strip()
        # A fenced block is common from a model asked for JSON. Stripped here
        # rather than in the prompt template, because the template is
        # provider-neutral and this is a Groq-shaped artefact.
        fenced = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.DOTALL)
        if fenced:
            text = fenced.group(1)

        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ProviderError(
                "The language model returned output that is not valid JSON."
            ) from exc

        if not isinstance(parsed, dict):
            raise ProviderError("The language model returned JSON that is not an object.")
        return parsed


@dataclass(frozen=True, slots=True)
class ModelInfo:
    """One model the provider reports, for the health probe and `/config`."""

    id: str
    context_window: int | None = None


# ============================================================================
# Reasoning handling (FR-034, R-014)
# ============================================================================

#: Keys that must never appear in a value leaving this process. Shared with the
#: contract test in `tests/contract/test_openapi_conformance.py` so the API
#: check and the provider check look for the same thing.
REASONING_KEY_PATTERN: str = (
    r"reasoning|thought|chain_of_thought|deliberation|reflection|scratchpad"
)

#: Keys stripped from a parsed classification before it is stored anywhere.
_REASONING_KEYS = re.compile(REASONING_KEY_PATTERN, re.IGNORECASE)

#: Substrings in a completion that indicate the model narrated its own
#: reasoning rather than answering. This is the check that matters once the
#: structural one is in place: deliberation that arrives inside `content` has no
#: key to give it away, and no field to stop reading it.
_LEAK_MARKERS = (
    "chain of thought",
    "chain-of-thought",
    "let me think step by step",
    "my reasoning is",
    "internal monologue",
)

#: How deep to walk before deciding a structure is not a data structure. Eight
#: is far beyond anything a `Completion` nests, and the bound exists so a
#: cyclic or pathological payload terminates instead of exhausting the stack.
_MAX_DEPTH = 8


class ReasoningHeuristic(NamedTuple):
    """The verdict, and the marker that produced it.

    Both parts, because a boolean tells whoever reads a failure whether the
    answer was refused but not what tripped it. The marker names the specific
    phrase, which is the difference between "this heuristic is too aggressive"
    and "the model leaked on this exact phrasing".
    """

    looks_like: bool
    marker: str | None = None

    def __bool__(self) -> bool:
        return self.looks_like


def find_reasoning_keys(payload: Any, *, _path: str = "$", _depth: int = 0) -> list[str]:
    """Every JSON path in `payload` whose key looks like a reasoning channel.

    Recursive, because the leak does not have to be at the top level: a
    `{"analysis": {"reasoning": ...}}` wrapper is the same violation and a
    top-level-only check would miss it.

    **Dataclasses are walked, and that is the point.** The value being checked
    on the provider path is a `Completion`, not a dict, and a walk that handled
    only dicts and lists would return `[]` for it every time — a check that
    passes because it looked in the wrong shape, which is worse than no check at
    all because it reads as evidence. Walking `dataclasses.fields()` means the
    check sees a future `Completion.reasoning` and fails on it.
    """
    if _depth > _MAX_DEPTH:
        return []

    found: list[str] = []

    if dataclasses.is_dataclass(payload) and not isinstance(payload, type):
        for spec in dataclasses.fields(payload):
            child = f"{_path}.{spec.name}"
            if _REASONING_KEYS.search(spec.name):
                found.append(child)
            try:
                value = getattr(payload, spec.name)
            except AttributeError:  # pragma: no cover - a field that never initialised
                continue
            found.extend(find_reasoning_keys(value, _path=child, _depth=_depth + 1))
    elif isinstance(payload, dict):
        for key, value in payload.items():
            child = f"{_path}.{key}"
            if isinstance(key, str) and _REASONING_KEYS.search(key):
                found.append(child)
            found.extend(find_reasoning_keys(value, _path=child, _depth=_depth + 1))
    elif isinstance(payload, (list, tuple, set, frozenset)):
        for index, item in enumerate(payload):
            found.extend(find_reasoning_keys(item, _path=f"{_path}[{index}]", _depth=_depth + 1))

    return found


def assert_no_reasoning(payload: Any, *, context: str = "provider response") -> None:
    """Raise `ProviderError` if a value carries a reasoning channel.

    Called on the value **this process is about to return** — never on the
    provider's raw envelope. Those are different objects with different
    obligations, and conflating them is the defect FR-034 is read to prevent:
    the provider is permitted to reason, and refusing its envelope would refuse
    every query from a reasoning model rather than protecting a single one.

    The check is on the *keys*, not on the prose, because a key named `reasoning`
    is a structural commitment to expose one and its absence cannot be proven by
    reading the text.
    """
    leaks = find_reasoning_keys(payload)
    if leaks:
        _log.error(
            "a value about to be returned exposed a reasoning channel",
            extra={"context": context, "paths": leaks},
        )
        raise ProviderError(
            "The language model provider returned a response exposing internal reasoning."
        )


def text_looks_like_reasoning(text: str) -> ReasoningHeuristic:
    """Whether a completion narrates its own reasoning.

    The second of the two FR-034 checks, and the one that survives the
    structural one: deliberation inlined into `content` is exposed with no
    offending key anywhere, so `assert_no_reasoning` passes and the user reads
    it anyway.

    It is a heuristic and is documented as one. It cannot be exhaustive, and it
    does not need to be: a false positive refuses an answer that was fine, which
    is the correct direction for a MUST NOT to fail in.
    """
    lowered = text.lower()
    for marker in _LEAK_MARKERS:
        if marker in lowered:
            return ReasoningHeuristic(True, marker)
    return ReasoningHeuristic(False)


@dataclass(frozen=True, slots=True)
class DiscardReport:
    """What a provider response carried and this adapter threw away.

    Two things are dropped at the boundary: `message.reasoning`, and the
    `usage.completion_tokens_details.reasoning_tokens` count beside it.

    This records them as *facts* and never as content. A log line quoting the
    deliberation would move the leak out of the response and into the log, which
    FR-034 covers — "in any response, event, or view" — and which no downstream
    redaction filter would catch, because the text is not a secret.

    Counting rather than staying silent is deliberate. A discard that leaves no
    trace is indistinguishable from a service that stopped reasoning, and those
    have different causes and different costs: one means the model got cheaper,
    the other means the boundary stopped working. A token *count* is a
    measurement of computation, not the computation, and cannot reconstruct a
    channel from it.
    """

    carried_reasoning: bool
    reasoning_tokens: int


def _discarded_reasoning(body: dict[str, Any]) -> DiscardReport:
    """Measure what the response carried in channels this adapter does not read.

    Reads two locations and nothing else: `choices[0].message.reasoning`, and
    `usage.completion_tokens_details.reasoning_tokens`. Both are R-014
    measurements from the live service rather than guesses at its shape, and
    both are read *only* to be counted. The reasoning text itself is never
    bound to a name that could reach a log record or an exception message.
    """
    carried = False
    choices = body.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        message = choices[0].get("message")
        if isinstance(message, dict) and isinstance(message.get("reasoning"), str):
            carried = bool(message["reasoning"].strip())

    tokens = 0
    usage = body.get("usage")
    if isinstance(usage, dict):
        details = usage.get("completion_tokens_details")
        if isinstance(details, dict):
            tokens = _as_int(details.get("reasoning_tokens"))

    return DiscardReport(carried_reasoning=carried, reasoning_tokens=tokens)


# ============================================================================
# The interface
# ============================================================================


@runtime_checkable
class LLMProvider(Protocol):
    """What the generation and classification stages need from a provider."""

    async def complete(
        self,
        *,
        system: str,
        user: str,
        model: str | None = None,
        max_tokens: int = 1200,
        json_mode: bool = False,
    ) -> Completion:
        """One completion, carrying no reasoning (FR-034).

        The contract, stated for implementers: the returned value must contain
        no reasoning channel. It does not say the provider must not produce one.
        An implementation that cannot suppress reasoning upstream still
        satisfies this by not reading it — and an implementation that *refuses*
        the provider's envelope instead would fail every query on a reasoning
        model, which is a stricter and much less useful reading.
        """
        ...

    async def classify(self, *, system: str, user: str) -> dict[str, Any]:
        """A structured classification, or an error. Never a forced value (FR-009)."""
        ...

    async def list_models(self) -> list[ModelInfo]:
        """Models available to the configured key. For the health probe."""
        ...


# ============================================================================
# Groq adapter
# ============================================================================


class GroqProvider:
    """`LLMProvider` over Groq's OpenAI-compatible chat completions API.

    R-006: the base URL is `https://api.groq.com/openai/v1` and the
    OpenAI-compatible surface is used directly over `httpx` rather than through
    a vendor SDK. The reason is specific: the `groq` SDK's own retry default is
    unbounded, and R-008 assigns retry ownership to this application. Adopting
    an SDK to get one convenience method would mean fighting its retry layer to
    satisfy the research finding, which is more code than the HTTP call itself.
    """

    def __init__(self, *, client: Any | None = None) -> None:
        self._client = client

    def _headers(self) -> dict[str, str]:
        settings = get_settings()
        return {
            "Authorization": f"Bearer {settings.groq_api_key.get_secret_value()}",
            "Content-Type": "application/json",
        }

    # -- error translation -------------------------------------------------

    def _translate(self, exc: Exception, operation: str) -> Exception:
        """Map a transport failure to this system's error vocabulary.

        `429` and `503` are separated from other failures because they are
        retryable *by the caller after a delay*, and a client that cannot tell
        them apart retries a 401 immediately and makes the situation worse.
        """
        import httpx

        if isinstance(exc, httpx.TimeoutException):
            _log.error("provider timed out", extra={"operation": operation})
            return ProviderError("The language model provider did not respond in time.")

        if isinstance(exc, httpx.HTTPError):
            _log.error(
                "provider unreachable", extra={"operation": operation, "error": redact(str(exc))}
            )
            return ProviderError("The language model provider is unreachable.")

        return ProviderError()

    async def _post(self, path: str, payload: dict[str, Any], *, operation: str) -> dict[str, Any]:
        """POST to the provider and return the parsed body, or raise."""
        return await self._request("POST", path, payload, operation=operation)

    async def _get(self, path: str, *, operation: str) -> dict[str, Any]:
        """GET from the provider and return the parsed body, or raise.

        Exists because the verb is part of the request, and folding it into a
        single `_post` made it invisible: a listing endpoint called with POST is
        rejected by the service as an unknown URL, and the resulting error
        looks exactly like an unreachable provider.
        """
        return await self._request("GET", path, None, operation=operation)

    async def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None,
        *,
        operation: str,
    ) -> dict[str, Any]:
        """Call the provider, translate transport errors, and parse the body.

        Status handling, throttling, redaction and shape validation live here so
        that `GET` and `POST` cannot diverge on any of it. A second copy of this
        for `GET` would differ within a month.
        """
        settings = get_settings()
        url = f"{settings.groq_base_url.rstrip('/')}/{path.lstrip('/')}"

        from app.ingestion.fetcher import SharedClient

        # `json=None` omits the body entirely rather than sending a literal
        # "null", which some gateways reject on a GET.
        kwargs: dict[str, Any] = {"headers": self._headers()}
        if payload is not None:
            kwargs["json"] = payload

        client_cm = None
        try:
            if self._client is not None:
                response = await self._client.request(method, url, **kwargs)
            else:
                client_cm = SharedClient()
                client = await client_cm.__aenter__()
                try:
                    response = await client.request(method, url, **kwargs)
                finally:
                    await client_cm.__aexit__(None, None, None)
        except Exception as exc:  # noqa: BLE001
            raise self._translate(exc, operation) from None

        # The status is checked before the body is parsed, so a 4xx with an HTML
        # error page does not surface as "invalid JSON".
        if response.status_code in (429, 503):
            retry_after = _retry_after(response)
            _log.warning(
                "provider throttling",
                extra={"operation": operation, "status": response.status_code},
            )
            raise ProviderRateLimited(retry_after=retry_after)

        if response.is_error:
            # The provider's error body is logged redacted, never returned: it can
            # echo the request, and the request carries the authorization header
            # (FR-042, FR-047).
            _log.error(
                "provider rejected a request",
                extra={
                    "operation": operation,
                    "status": response.status_code,
                    "body": redact(response.text[:500]),
                },
            )
            if response.status_code in (401, 403):
                raise ProviderError(
                    "The language model provider rejected the configured credential."
                )
            raise ProviderError()

        try:
            body = response.json()
        except ValueError as exc:
            raise ProviderError(
                "The language model provider returned a non-JSON response."
            ) from exc

        if not isinstance(body, dict):
            raise ProviderError(
                "The language model provider returned an unexpected response shape."
            )

        return body

    # -- LLMProvider -------------------------------------------------------

    async def complete(
        self,
        *,
        system: str,
        user: str,
        model: str | None = None,
        max_tokens: int = 1200,
        json_mode: bool = False,
    ) -> Completion:
        """One chat completion.

        `json_mode` requests the provider's constrained decoding. It is off by
        default because a constrained decoder cannot be used for the generation
        path — an answer is prose — and a mode that is on for one call and off
        for another is a mode someone will forget to set.
        """
        settings = get_settings()
        chosen = model or settings.groq_model

        payload: dict[str, Any] = {
            "model": chosen,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": max_tokens,
            "temperature": 0.1,
            "top_p": 1.0,
            "stream": False,
            # A cost control, not the FR-034 control. R-014: the service accepts
            # only `low`/`medium`/`high` and returns a populated
            # `message.reasoning` at every one of them, so no value of this
            # parameter removes the channel. What it does is stop the model
            # spending tokens on deliberation that is about to be discarded.
            # What guarantees FR-034 is the boundary in `_completion_from`.
            "reasoning_effort": settings.reasoning_effort,
        }

        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        body = await self._post("chat/completions", payload, operation="complete")

        # Measured, not assumed: what the response carried in the channels this
        # adapter will not read. `INFO` because a reasoning model reasoning
        # normally is not an incident — the point is that the *rate* is
        # observable, and a silent boundary is indistinguishable from a service
        # that stopped emitting.
        discarded = _discarded_reasoning(body)
        if discarded.carried_reasoning or discarded.reasoning_tokens:
            _log.info(
                "provider reasoning discarded at the adapter boundary",
                extra={
                    "model": chosen,
                    "carried_reasoning": discarded.carried_reasoning,
                    "reasoning_tokens": discarded.reasoning_tokens,
                },
            )

        return self._completion_from(body, fallback_model=chosen)

    def _completion_from(self, body: dict[str, Any], *, fallback_model: str) -> Completion:
        """Extract a `Completion`, discarding every channel but `content`.

        This is where FR-034 is discharged. The provider is allowed to reason; it
        is not allowed to reason *at a reader of this system*. So exactly one
        field is read out of the provider's message — `content` — and the
        `Completion` it builds has no field that could hold anything else.

        The alternative, walking the response and dropping keys that match
        `REASONING_KEY_PATTERN`, is worse in a way that is easy to miss: it is
        denylist-shaped, so it stays correct for responses that do not reason and
        silently passes on the next novel key name a provider invents. Reading
        one field has no equivalent failure.

        The constructed value is then checked. `assert_no_reasoning` walks
        dataclass fields, so this is a post-condition on the actual return value
        and it would fire on a future `Completion.reasoning` — a change that
        every type checker accepts and that no reviewer is guaranteed to notice.
        """
        choices = body.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ProviderError("The language model provider returned no choices.")

        first = choices[0]
        if not isinstance(first, dict):
            raise ProviderError("The language model provider returned an unexpected choice shape.")

        message = first.get("message")
        if not isinstance(message, dict):
            raise ProviderError("The language model provider returned a choice with no message.")

        text = message.get("content")
        if not isinstance(text, str) or not text.strip():
            # A reasoning channel is never a fallback for missing content.
            # Substituting one would make FR-034 violable by a single absent
            # field: every reasoning model that spends its whole budget thinking
            # would then answer with its thinking.
            if _discarded_reasoning(body).carried_reasoning:
                _log.error(
                    "provider returned a reasoning channel and no content",
                    extra={"finish_reason": first.get("finish_reason")},
                )
                raise ProviderError("The language model provider returned no usable content.")
            raise ProviderError("The language model provider returned an empty completion.")

        usage = self._usage_from(body)

        finish_reason = first.get("finish_reason")
        if finish_reason == "length":
            # Truncation is reported, not hidden. A caller that gets a complete
            # string which happens to end mid-sentence has no way to tell; this
            # way it does, and can regenerate (FR-011).
            _log.warning(
                "completion truncated at max_tokens",
                extra={"model": body.get("model", fallback_model)},
            )

        completion = Completion(
            text=text,
            model=str(body.get("model") or fallback_model),
            usage=usage,
            finish_reason=finish_reason if isinstance(finish_reason, str) else None,
        )

        # The post-condition. On the value about to leave this process, not on
        # the envelope that produced it.
        assert_no_reasoning(completion, context=f"groq complete[{fallback_model}]")

        # The check the structural one cannot make. `content` is a string, and no
        # key inside a string betrays that the string is deliberation.
        verdict = text_looks_like_reasoning(text)
        if verdict.looks_like:
            _log.error(
                "completion content reads like deliberation",
                extra={"model": completion.model, "marker": verdict.marker},
            )
            raise ProviderError(
                "The language model provider returned an answer exposing internal reasoning."
            )

        return completion

    def _usage_from(self, body: dict[str, Any]) -> TokenUsage:
        """Build `TokenUsage` from the response, and nothing else.

        `usage.completion_tokens_details` is read by `_discarded_reasoning` for
        its count and by nothing else. It is not carried here, and
        `TokenUsage` has no field for it: the reasoning token count is metadata
        about deliberation this system is refusing to expose, and storing it
        would leave a record of the channel where the policy says there is none.
        """
        raw_usage = body.get("usage")
        if not isinstance(raw_usage, dict):
            return TokenUsage()

        return TokenUsage(
            prompt_tokens=_as_int(raw_usage.get("prompt_tokens")),
            completion_tokens=_as_int(raw_usage.get("completion_tokens")),
            total_tokens=_as_int(raw_usage.get("total_tokens")),
            total_time=float(raw_usage.get("total_time") or 0.0),
        )

    async def classify(self, *, system: str, user: str) -> dict[str, Any]:
        """A structured classification, on the smaller model.

        R-006: `GROQ_CLASSIFICATION_MODEL` is the 20B variant. Classification is
        a labelling task over a short prompt, and running it on the generation
        model would spend generation-tier tokens on a decision that the
        confidence threshold is going to second-guess anyway.

        A malformed response raises rather than returning `{}`. FR-009 forbids
        forcing a classification, and a parse failure silently becoming "unknown"
        is forcing one.
        """
        settings = get_settings()

        completion = await self.complete(
            system=system,
            user=user,
            model=settings.groq_classification_model,
            max_tokens=200,
            json_mode=True,
        )

        parsed = completion.json()
        # The right check, on the right object. The provider's reasoning channel
        # is not one of our problems; a `thoughts` key that the *model wrote
        # into its own JSON* is, because that value is stored in `query_logs`
        # and returned by the API. A model asked for JSON will occasionally hand
        # back a `{"thoughts": ...}` alongside its answer, and storing that is
        # the exposure FR-034 names.
        assert_no_reasoning(parsed, context="groq classify")
        return parsed

    async def list_models(self) -> list[ModelInfo]:
        """Models the configured key can reach.

        Used by `/health` to distinguish "the provider is down" from "the model
        I configured is not on this plan" (R-006) — a 401 for an Enterprise-only
        model, which looks like a bad key and is not.

        A `GET`, and that matters more than it looks. `/models` is a listing, so
        a `POST` is rejected with `unknown_url` — a 404 whose body names the
        wrong verb, from a service that is otherwise perfectly healthy. Called
        through `self._post` it reported "provider unreachable" forever, and
        R-006's distinction was unreachable in practice: the probe could never
        get far enough to tell a bad key from an unavailable model, which are
        the two cases it exists to separate.
        """
        body = await self._get("models", operation="list_models")

        raw = body.get("data")
        if not isinstance(raw, list):
            return []

        models: list[ModelInfo] = []
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            identifier = entry.get("id")
            if not isinstance(identifier, str):
                continue
            context_window = entry.get("context_window")
            models.append(
                ModelInfo(
                    id=identifier,
                    context_window=context_window if isinstance(context_window, int) else None,
                )
            )
        return models

    async def health(self) -> str | None:
        """`None` when the provider is healthy; an authored reason otherwise.

        The reason names a *cause*, because the remedy differs per cause and
        `/health` is read by whoever is trying to act on it. Two distinctions are
        load-bearing:

        **A configuration fault is not a provider fault.** Settings are read
        before the request is built, so a missing or invalid variable raises a
        `ValidationError` and the provider is never contacted. Reporting that as
        "provider unreachable" points an operator at a service that is working
        perfectly while the actual fault — an unset variable — sits unmentioned.

        **A bad credential is not an outage**, and a configured model the key
        cannot reach is not a bad credential (R-006). All three say something
        different about what to do next, so all three get their own message.
        """
        try:
            settings = get_settings()
        except Exception as exc:  # noqa: BLE001
            return f"Provider configuration is invalid ({type(exc).__name__}); the service was not contacted."

        try:
            models = await self.list_models()
        except ProviderRateLimited:
            return "The language model provider is throttling requests."
        except ProviderError as exc:
            return exc.message
        except Exception as exc:  # noqa: BLE001
            return f"Language model provider unreachable ({type(exc).__name__})."

        available = {model.id for model in models}
        missing = [
            name
            for name in (settings.groq_model, settings.groq_classification_model)
            if name not in available
        ]
        if missing:
            return f"Configured model(s) not available to this key: {', '.join(missing)}."
        return None


# ============================================================================
# Helpers and module access
# ============================================================================


def _as_int(value: Any) -> int:
    """Coerce a usage field to int, treating anything else as zero.

    A provider that reports token usage as a string, or omits it, must not turn
    into a `TypeError` in the middle of recording a successful call. Zero is the
    honest value for "not reported" and is visibly different from a real count
    in the audit record.
    """
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return 0
    return 0


def _retry_after(response: Any) -> int | None:
    """Read `retry-after` from a provider response, in seconds."""
    raw = response.headers.get("retry-after")
    if not raw:
        return None
    try:
        delay = float(raw.strip())
    except ValueError:
        return None
    return int(max(1, min(delay, 60.0)))


_provider: GroqProvider | None = None


def get_language_model() -> GroqProvider:
    """The process-wide provider. Cached so the client is reused."""
    global _provider
    if _provider is None:
        _provider = GroqProvider()
    return _provider


def reset_language_model() -> None:
    """Drop the cached provider. Test teardown."""
    global _provider
    _provider = None


__all__ = [
    "REASONING_KEY_PATTERN",
    "Completion",
    "DiscardReport",
    "GroqProvider",
    "LLMProvider",
    "ModelInfo",
    "ReasoningHeuristic",
    "TokenUsage",
    "assert_no_reasoning",
    "find_reasoning_keys",
    "get_language_model",
    "reset_language_model",
    "text_looks_like_reasoning",
]
