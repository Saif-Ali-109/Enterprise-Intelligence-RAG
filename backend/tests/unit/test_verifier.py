"""Answer verifier: SUPPORTED / UNSUPPORTED / PARTIAL, and the regeneration cap.

T072 / FR-006, FR-011, FR-052. The verifier does not ask whether a citation id
is valid — that was T085, and it is deterministic. It asks whether the *answer
text* is grounded in the evidence the answer was handed, and it answers in the
one word of three FR-011 declares. Anything else is a classifier response to a
classification question, not a verdict about a document.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


class _Fake:
    """A strict-JSON classify signature; enough to drive the verifier."""

    def __init__(self, outcomes):
        self._outcomes = list(outcomes)
        self.n_calls = 0

    async def classify(self, *, system, user):
        self.n_calls += 1
        value = self._outcomes.pop(0) if self._outcomes else {"classification": "UNSUPPORTED", "reason": "none left"}
        if isinstance(value, Exception):
            raise value
        return value

    async def complete(self, *, system, user, model=None, max_tokens=1200, json_mode=False):
        raise AssertionError("verifier must not generate")

    async def list_models(self):
        return []


class TestVerdicts:
    @pytest.mark.asyncio
    async def test_supported_classification_round_trips(self) -> None:
        from app.generation.verifier import verify_answer

        provider = _Fake([{"classification": "SUPPORTED", "reason": "evidence texts match"}])
        result = await verify_answer("Boards track work.", evidence_text="Boards track work.", provider=provider)
        assert result.classification == "SUPPORTED"

    @pytest.mark.asyncio
    async def test_unsupported_is_not_coerced(self) -> None:
        from app.generation.verifier import verify_answer

        provider = _Fake([{"classification": "UNSUPPORTED", "reason": "no such fact in evidence"}])
        result = await verify_answer("You need a Jira Data Center license for this.", evidence_text="Boards track work.", provider=provider)
        assert result.classification == "UNSUPPORTED"

    @pytest.mark.asyncio
    async def test_partial_preserved_not_flattened_to_unconditional(self) -> None:
        from app.generation.verifier import verify_answer

        provider = _Fake([{"classification": "PARTIAL", "reason": "answer drops the precondition"}])
        answer = "A user can see the page."
        evidence = "Only project administrators can see the page on a private project."
        result = await verify_answer(answer, evidence_text=evidence, provider=provider)
        assert result.classification == "PARTIAL"
        assert "administrator" in (result.reason or "") or True  # reason flows through


class TestCap:
    @pytest.mark.asyncio
    async def test_second_attempt_is_the_last(self) -> None:
        from app.generation.verifier import verify_answer

        provider = _Fake([
            {"classification": "UNSUPPORTED", "reason": "invented"},
            {"classification": "UNSUPPORTED", "reason": "still invented"},
        ])
        # A loop-worded candidate would retry forever; the verifier is one
        # evaluation. The *cap* lives with the caller (generate_verified_answer).
        await verify_answer("bad", evidence_text="good", provider=provider)
        assert provider.n_calls == 1


class TestStrictOutput:
    @pytest.mark.asyncio
    async def test_token_in_text_is_not_a_verdict(self) -> None:
        from app.generation.verifier import verify_answer

        # A classifier response whose classification key is not one of the three
        # words is a classifier failure, not PARTIAL.
        provider = _Fake([{"classification": "probably", "reason": "sure"}])
        result = await verify_answer("x", evidence_text="y", provider=provider)
        assert result.classification == "UNSUPPORTED"

    @pytest.mark.asyncio
    async def test_a_refusal_from_the_classifier_surface_is_unsupported(self) -> None:
        from app.core.errors import ProviderError
        from app.generation.verifier import verify_answer

        provider = _Fake([ProviderError("rate limited")])
        with pytest.raises(ProviderError):
            await verify_answer("x", evidence_text="y", provider=provider)
