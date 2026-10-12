"""A throttled provider must not silently shrink a run's measurements.

Measured 2026-10-09 on run `fb88cffa`: 5 of 32 questions raised
`PROVIDER_RATE_LIMITED` from a free-tier quota part-way through, so every quality
metric was computed over 27 questions and p95 carried the throttling. The runner
retries a throttled question; the request path does not, because a user waiting on
one question is better served by being told to try again.

These tests pin the *policy*, not the timing: the delay is set to zero so the
suite stays fast, and what is asserted is which failures are retried, how many
times, and what is finally reported.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from app.core.errors import ErrorCode, ProviderError, ProviderRateLimited
from app.evaluation.runner import _with_throttle_retry

pytestmark = pytest.mark.unit

SETTINGS = SimpleNamespace(eval_question_retries=2, eval_retry_delay_seconds=0.0)


def throttled() -> ProviderRateLimited:
    """The error the provider adapter raises on a 429, constructed as it is there."""
    return ProviderRateLimited()


def provider_error() -> ProviderError:
    return ProviderError()


async def test_a_throttled_question_is_retried() -> None:
    """Once is enough for the common case, and the second attempt answers."""
    calls = 0

    async def operation() -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise throttled()
        return "answered"

    assert await _with_throttle_retry(operation, question="q1", settings=SETTINGS) == "answered"
    assert calls == 2


async def test_the_retry_budget_is_bounded() -> None:
    """A throttle that persists is reported, not retried forever.

    `eval_question_retries = 2` means three attempts in total: the original and
    two retries. An unbounded retry would turn a quota exhaustion into a run that
    never finishes, which is worse than a run with an error row in it.
    """
    calls = 0

    async def operation() -> str:
        nonlocal calls
        calls += 1
        raise throttled()

    with pytest.raises(ProviderRateLimited) as caught:
        await _with_throttle_retry(operation, question="q1", settings=SETTINGS)

    assert calls == 3
    assert caught.value.code is ErrorCode.PROVIDER_RATE_LIMITED


async def test_a_provider_error_is_not_retried() -> None:
    """Repeating an unusable response measures the provider's mood, not the system."""
    calls = 0

    async def operation() -> str:
        nonlocal calls
        calls += 1
        raise provider_error()

    with pytest.raises(ProviderError) as caught:
        await _with_throttle_retry(operation, question="q1", settings=SETTINGS)

    assert calls == 1
    assert caught.value.code is ErrorCode.PROVIDER_ERROR


async def test_retries_can_be_switched_off() -> None:
    """`eval_question_retries = 0` is a real setting, not a disabled one.

    This is what makes the retry testable rather than assumed: a test that asserts
    "it retried" also needs to be able to assert "with retries off, it does not",
    or the first assertion proves nothing about the knob.
    """
    calls = 0

    async def operation() -> str:
        nonlocal calls
        calls += 1
        raise throttled()

    with pytest.raises(ProviderRateLimited):
        await _with_throttle_retry(
            operation,
            question="q1",
            settings=SimpleNamespace(eval_question_retries=0, eval_retry_delay_seconds=0.0),
        )
    assert calls == 1


async def test_a_success_on_the_first_attempt_calls_once() -> None:
    """The retry must not cost anything on the ordinary path."""
    calls = 0

    async def operation() -> str:
        nonlocal calls
        calls += 1
        return "answered"

    assert await _with_throttle_retry(operation, question="q1", settings=SETTINGS) == "answered"
    assert calls == 1


async def test_an_unrelated_exception_propagates_unchanged() -> None:
    """A bug in the pipeline must not be retried into a different bug."""
    calls = 0

    async def operation() -> str:
        nonlocal calls
        calls += 1
        raise ValueError("a defect, not a throttle")

    with pytest.raises(ValueError, match="a defect, not a throttle"):
        await _with_throttle_retry(operation, question="q1", settings=SETTINGS)
    assert calls == 1
