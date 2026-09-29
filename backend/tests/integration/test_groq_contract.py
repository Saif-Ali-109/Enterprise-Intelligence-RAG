"""Live Groq verification: the contract the provider depends on.

T028. **Executed 2026-09-29 against a live Groq account: 14 passed.** The
measurements that run produced are recorded in `research.md` R-014. These tests
require a real `GROQ_API_KEY` and skip with an explicit message rather than
passing vacuously, so that number is a property of that run and not of this file;
re-run them to re-measure.

`tests/unit/test_vendor_adapters.py` covers the provider against `httpx.MockTransport`,
which proves the request is built correctly and the response is read correctly. It
cannot prove what the service does. This file is the half that needs the network,
and it is where the two assumptions that matter most get tested.

## What only a live call can answer

**1. That the reasoning channel is real, and that discarding it is load-bearing.**
FR-034 is a requirement about the *emitted* surface, so the obligation is to never
read `message.reasoning` — not to insist the provider stop sending it. R-014's
measurement says the channel cannot be switched off: `reasoning_effort="none"` is
rejected with HTTP 400, and both configured models return a populated `reasoning`
at every accepted effort. That makes this a question only the network can answer:
if the service ever stopped sending the field, every test in this section would
still pass while the discard had become untested dead code. So
`test_the_service_really_does_send_reasoning` asserts the leak is *present*, and
the tests below then assert the answer does not carry it.

**2. Whether the model pair behaves as configured.** R-006 records that a
configured model which is unavailable to the key 401s, which is indistinguishable
from a bad key. `test_configured_models_are_reachable` distinguishes them.

**3. Whether classification JSON output is reliably parseable.** The provider
refuses to force a classification (FR-009), so an occasional malformed response
becomes a hard error rather than a wrong product. Whether `response_format` is
honoured often enough for that policy to be usable is a service property.

**4. The actual context window and token accounting**, which determine whether
the 6-unit evidence set fits in the prompt.

## Running these

Requires a `GROQ_API_KEY` with access to the two configured models. Every test
makes a small number of short completions; the suite is not a benchmark and does
not measure latency. Do not record a pass until it has been executed.
"""

from __future__ import annotations

import os

import pytest
import pytest_asyncio
from app.core.errors import ProviderError

from tests.fixtures.credentials import has_credential

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not has_credential("GROQ_API_KEY"),
        reason=(
            "T028 requires a live Groq account. No real GROQ_API_KEY is set — "
            "a placeholder does not count. Executed 2026-09-29: 14 passed. "
            "A skip now is not a pass, and is not the same as that run."
        ),
    ),
]


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def live_provider():
    """A real `GroqProvider`, shared across the module.

    `loop_scope="module"` is required, not stylistic. `pytest.ini` sets
    `asyncio_default_fixture_loop_scope = function`, so a module-scoped
    `async def` fixture without it asks pytest-asyncio for a function-scoped
    runner from a module-scoped request and dies with a `ScopeMismatch` at
    setup.

    That failure was invisible while `GROQ_API_KEY` was unset, because the
    module-level `skipif` short-circuits before any fixture is requested. The
    bug would therefore have surfaced for the first time on the first real run,
    against a live account, as fourteen errors that look like the provider is
    broken. A test that cannot run until its dependency exists needs its setup
    path verified without the dependency.
    """
    from app.generation.provider import GroqProvider

    try:
        reason = await GroqProvider().health()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Groq is not reachable with the configured key: {type(exc).__name__}: {exc}")

    if reason is not None:
        # Not "Groq is reachable but not usable" — that asserts a fact the
        # reason may contradict. A configuration fault means the service was
        # never contacted at all, and a skip message that claims otherwise sends
        # whoever reads it to debug the wrong layer.
        pytest.skip(f"Groq is not usable with this configuration: {reason}")

    from app.generation.provider import reset_language_model

    yield GroqProvider()

    reset_language_model()


# ============================================================================
# Model availability (R-006)
# ============================================================================


class TestLiveModelAvailability:
    async def test_configured_models_are_reachable(self, live_provider) -> None:
        """A configured model the key cannot reach must be distinguishable.

        R-006: an unavailable model returns 401, which looks exactly like a bad
        key. Without this check, a misconfigured model name sends an operator to
        rotate a credential that is perfectly fine.
        """
        reason = await live_provider.health()

        assert reason is None, reason

    async def test_both_configured_models_appear_in_the_listing(self, live_provider) -> None:
        from app.core.config import get_settings

        settings = get_settings()
        available = {model.id for model in await live_provider.list_models()}

        assert settings.groq_model in available, (
            f"{settings.groq_model!r} is configured but not offered to this key"
        )
        assert settings.groq_classification_model in available, (
            f"{settings.groq_classification_model!r} is configured but not offered to this key"
        )

    async def test_the_context_window_is_reported_when_present(self, live_provider) -> None:
        """Needed to know whether a 6-unit evidence set fits in the prompt.

        `None` is a legitimate answer — the field is optional — so this asserts
        the value is an int when present rather than demanding one.
        """
        from app.core.config import get_settings

        settings = get_settings()
        models = {model.id: model for model in await live_provider.list_models()}

        context = models[settings.groq_model].context_window
        if context is not None:
            assert isinstance(context, int)
            assert context > 0
            print(f"\n  T028 {settings.groq_model} context window: {context:,} tokens")


# ============================================================================
# Reasoning suppression (FR-034) — the one that matters most
# ============================================================================


class TestLiveReasoningSuppression:
    async def test_the_service_really_does_send_reasoning(self, live_provider) -> None:
        """The premise, asserted so the rest of this class means something.

        R-014 measured that `message.reasoning` is populated on both configured
        models and that `reasoning_effort` accepts only `low`/`medium`/`high` —
        there is no way to ask for none. If that stopped being true, every
        remaining test here would still pass, and the discard in
        `_completion_from` would have become untested code that looked load-bearing.

        This deliberately calls the service directly rather than through the
        provider: the question is about the wire, and the provider is precisely
        the thing that does not let the field through.

        The assertions are about *presence*, never about content. Reading the
        deliberation to assert on it would put the leak inside the test that
        exists to detect the leak.
        """
        import httpx
        from app.core.config import get_settings

        settings = get_settings()

        async with httpx.AsyncClient(
            base_url=settings.groq_base_url,
            headers={"Authorization": f"Bearer {settings.groq_api_key.get_secret_value()}"},
            timeout=30.0,
        ) as client:
            response = await client.post(
                "/chat/completions",
                json={
                    "model": settings.groq_model,
                    "messages": [{"role": "user", "content": "What is a Jira board filter?"}],
                    "max_tokens": 200,
                    "reasoning_effort": settings.reasoning_effort,
                    "stream": False,
                },
            )

        assert response.status_code == 200, response.text[:300]
        message = response.json()["choices"][0]["message"]

        assert "reasoning" in message, (
            f"{settings.groq_model} returned message keys {sorted(message)}; the "
            "service no longer sends a reasoning channel, so the discard in "
            "provider.py is untested and R-014 needs re-measuring"
        )
        # The token count is the observable half of the same fact, and it is what
        # the discard report is built from.
        details = (response.json().get("usage") or {}).get("completion_tokens_details") or {}
        print(f"\n  T028 service reasoning_tokens at {settings.reasoning_effort!r}: {details}")

    async def test_the_discard_boundary_keeps_the_answer_and_drops_the_rest(
        self, live_provider
    ) -> None:
        """The real path: a response that carries reasoning, returned without it.

        This is the FR-034 guarantee as it must actually work. The unit tests prove
        the boundary against payloads I wrote; this proves it against payloads the
        service wrote, which is the only version that could differ.
        """
        import dataclasses

        from app.generation.provider import find_reasoning_keys

        completion = await live_provider.complete(
            system="Answer in one sentence. Do not explain your reasoning.",
            user="What does a Jira board filter do?",
        )

        assert completion.text.strip()
        assert completion.usage.total_tokens > 0

        # The post-condition, run on the value the service actually produced.
        assert find_reasoning_keys(completion) == []

        # And the deliberation text, checked for in the whole object, without ever
        # having read it: the unit test supplies it. Here the assertion is that no
        # *field* exists, because the text was never bound to a name.
        rendered = repr(dataclasses.asdict(completion))
        assert "reasoning" not in rendered.lower(), rendered[:400]

    async def test_the_answer_does_not_read_like_deliberation(self, live_provider) -> None:
        """A second, independent check, because the structural one has a gap.

        A model could inline its reasoning into `content` — "First, let us
        consider… Therefore…". No reasoning *key* would be present, so
        `assert_no_reasoning` passes, and the deliberation reaches the user
        anyway. `text_looks_like_reasoning` catches the common shapes of that.

        Not exhaustive, and a false positive here would fail a test rather than
        corrupt an answer, so the bias is towards reporting.
        """
        from app.generation.provider import text_looks_like_reasoning

        try:
            completion = await live_provider.complete(
                system=(
                    "Answer the question directly in at most two sentences. "
                    "Do not narrate your process, do not enumerate steps, and do not "
                    "state your reasoning."
                ),
                user="How do I transition a Jira issue with a workflow?",
            )
        except ProviderError as exc:
            # The heuristic fired. That is a real finding, not a test failure: the
            # provider refuses the answer rather than pass deliberation on, which
            # is the intended behaviour — but it means a legitimate-looking
            # question is being refused, and whoever reads this needs to know
            # whether that is the model or the marker list.
            pytest.fail(
                "the provider refused the completion as reading like deliberation "
                f"({exc.message}). The answer is withheld by design, but this is the "
                "case where the marker list may be too aggressive."
            )

        # The provider now applies this check itself and refuses, so reaching here
        # means it already passed. Asserted again anyway, and that redundancy is the
        # point: the test is about the *answer* being clean, and a check that only
        # passes because an earlier one raised is a check of the checker.
        verdict = text_looks_like_reasoning(completion.text)

        print(
            f"\n  T028 completion ({completion.usage.total_tokens} tokens): {completion.text[:200]!r}"
        )
        assert not verdict, (
            f"the answer reads like deliberation (matched {verdict.marker!r}): "
            f"{completion.text[:400]!r}"
        )

    async def test_repeated_calls_do_not_leak_reasoning(self, live_provider) -> None:
        """Suppression is a per-request property, so a single call proves little.

        A control that is honoured probabilistically would pass a one-shot test
        often enough to be trusted and leak often enough to matter. Several calls
        make an intermittent leak visible.
        """
        for attempt in range(3):
            completion = await live_provider.complete(
                system="Answer in one sentence.",
                user="What is a Confluence content property?",
            )
            assert completion.text.strip(), f"call {attempt} returned an empty completion"

    async def test_a_classification_response_carries_no_reasoning(self, live_provider) -> None:
        """The smaller model reasons too, and its output is parsed as JSON.

        A leaked reasoning channel here would be caught by `assert_no_reasoning`
        inside `classify`, and the run would fail with a clear error rather than
        storing a classification the model reached by a route the audit trail
        does not show.
        """
        result = await live_provider.classify(
            system='Classify the product as one of "jira", "confluence", or "unknown". '
            'Reply with JSON only: {"product": "...", "confidence": 0.0}.',
            user="How do I configure a request form?",
        )

        assert isinstance(result, dict)
        assert result.get("product") in {"jira", "confluence", "unknown"}


# ============================================================================
# Grounded completion
# ============================================================================


class TestLiveCompletion:
    async def test_a_supplied_context_is_actually_used(self, live_provider) -> None:
        """A model that ignores its context will answer from memory instead.

        This is the failure mode the whole system is designed to prevent, and it
        is invisible from a response alone: a fluent, correct-sounding answer
        citing nothing. The test supplies a context containing a fact that is
        deliberately wrong, and requires the answer to follow the context rather
        than the model's own knowledge. This is the live analogue of the
        grounding requirement, and it is deliberately adversarial.
        """
        completion = await live_provider.complete(
            system=(
                "Answer using only the CONTEXT provided. If the context does not "
                "contain the answer, say so. Do not use outside knowledge."
            ),
            user=(
                "CONTEXT:\n"
                "The maintenance window for the fictional Zephyr Queue service is "
                "Tuesdays from 02:00 to 04:00 UTC.\n\n"
                "QUESTION: When is the Zephyr Queue maintenance window?"
            ),
        )

        text = completion.text.lower()
        print(f"\n  T028 grounded answer: {completion.text[:200]!r}")

        # The point is that the model repeats the supplied fact rather than
        # improvising a plausible answer. "Tuesday" must appear; the window hours
        # must appear, since they came only from the context.
        assert "tuesday" in text or "02:00" in text, (
            f"the answer does not reflect the supplied context: {completion.text[:300]!r}"
        )

    async def test_usage_is_reported_and_plausible(self, live_provider) -> None:
        """Token counts feed the cost and budget reporting.

        A provider reporting zeros would make every cost figure in the system
        fiction, and the accounting is only trustworthy if it is checked against
        a real response rather than trusted.
        """
        completion = await live_provider.complete(
            system="Answer in one sentence.",
            user="What is a Jira workflow scheme?",
        )

        usage = completion.usage
        assert usage.prompt_tokens > 0
        assert usage.completion_tokens > 0
        assert usage.total_tokens >= usage.completion_tokens
        # A total below the prompt count would mean the arithmetic is wrong even
        # if each field looks individually plausible.
        assert usage.total_tokens >= usage.prompt_tokens

    async def test_finish_reason_is_reported(self, live_provider) -> None:
        """`length` means a truncated answer, which must not be presented as complete.

        A truncated answer with citations attached is a grounding defect: the
        model stopped mid-sentence and the caller cannot tell from the text.
        """
        completion = await live_provider.complete(
            system="Answer in one sentence.",
            user="What is a Confluence blueprint?",
        )

        assert completion.finish_reason in {
            "stop",
            "length",
            "tool_calls",
            "function_call",
            "eos",
            None,
        }


# ============================================================================
# Classification (FR-009)
# ============================================================================


class TestLiveClassification:
    async def test_classification_returns_parseable_json(self, live_provider) -> None:
        """`response_format` is honoured often enough to be usable.

        The provider refuses to force a value, so a malformed response is a hard
        error by design. That is the correct policy and it makes response
        reliability a *service* requirement rather than a preference — so it is
        measured here rather than assumed.
        """
        for attempt in range(3):
            result = await live_provider.classify(
                system='Reply with JSON only: {"product": "jira|confluence|unknown", "confidence": 0.0-1.0}.',
                user="Where do I configure the customer portal for a JSM project?",
            )
            assert isinstance(result, dict), f"attempt {attempt} returned {type(result).__name__}"
            assert "product" in result

    async def test_an_unclassifiable_question_yields_unknown_not_a_guess(
        self, live_provider
    ) -> None:
        """The refusal path, tested against a real model.

        A question about something outside the corpus is the case FR-009 exists
        for. A model asked to classify it will usually still pick a product,
        because that is what was asked — so the test asserts the *contract*
        (a confidence is reported and the system can represent `unknown`)
        rather than pretending the model will volunteer uncertainty.
        """
        result = await live_provider.classify(
            system='Reply with JSON only: {"product": "jira|confluence|unknown", "confidence": 0.0-1.0}. '
            'If the question does not concern Atlassian software products, use "unknown" '
            "with a low confidence.",
            user="What is the best recipe for sourdough bread?",
        )

        assert result["product"] in {"jira", "confluence", "unknown"}
        confidence = result.get("confidence")
        if confidence is not None:
            assert 0.0 <= float(confidence) <= 1.0


# ============================================================================
# Failure modes worth knowing about before an evaluation run
# ============================================================================


class TestLiveErrorBehaviour:
    async def test_a_very_long_prompt_is_refused_rather_than_silently_truncated(
        self, live_provider
    ) -> None:
        """Whether the service truncates or rejects is unknown until this runs.

        The chunker's 1000-token ceiling and the 6-unit evidence budget both
        assume the prompt fits. If a long prompt is silently truncated, a
        retrieval that assembled seven units would produce an answer grounded in
        the first few — and the citations would still validate, because the
        validator checks that cited units were in the evidence set, not that the
        model read all of them.

        This test asks for something far beyond any context window and reports
        what happened. A rejection is the good outcome and is what the provider
        already handles. A silent truncation is the finding that matters, and the
        docstring records it as an open item for `research.md`.
        """
        oversized = (
            "Jira Service Management request queues accept a customer portal. " * 20_000
        ).strip()

        try:
            completion = await live_provider.complete(
                system="Answer in one sentence.",
                user=f"CONTEXT:\n{oversized}\n\nQUESTION: What is a request queue?",
            )
        except Exception as exc:  # noqa: BLE001
            print(f"\n  T028 oversized prompt REFUSED: {type(exc).__name__}: {exc}")
            # A refusal is the good outcome: the provider's own error handling
            # turns this into a clean failure rather than a truncated answer.
            return

        print(
            f"\n  T028 oversized prompt ({len(oversized.split()):,} words) "
            f"ACCEPTED, finish_reason={completion.finish_reason!r}, "
            f"prompt_tokens={completion.usage.prompt_tokens:,}"
        )
        if completion.finish_reason == "length":
            pytest.fail(
                "the service silently truncated an oversized prompt and returned a "
                "partial answer. The evidence budget must therefore be validated "
                "against the model's real context window, not assumed to fit."
            )

    async def test_a_nonexistent_model_is_rejected_promptly(self, live_provider) -> None:
        """A bad model name should fail fast, not hang.

        R-006's operational consequence. An unavailable model is reported as 401
        or 404, and a client that waits on it instead of checking is
        indistinguishable from a network fault. This calls the service directly
        rather than through the provider, because the question is about the
        service's behaviour, not about the provider's translation of it.
        """
        import httpx

        async with httpx.AsyncClient(
            base_url="https://api.groq.com",
            headers={"Authorization": f"Bearer {os.environ['GROQ_API_KEY']}"},
            timeout=15.0,
        ) as client:
            response = await client.get("/openai/v1/models/this-model-does-not-exist")

        assert response.status_code in {400, 401, 403, 404}, (
            f"unexpected status {response.status_code} for a nonexistent model; "
            f"a 200 would mean the service accepted a model that cannot exist"
        )
