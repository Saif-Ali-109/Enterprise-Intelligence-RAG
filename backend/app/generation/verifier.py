"""Answer verifier: classification of SUPPORTED / UNSUPPORTED / PARTIAL (T087 / FR-011).

The classifier is asked exactly one question against exactly the evidence the
answer was handed. Its verdict is the third deliverable the user is told about:
valid, present, and grounded. A classifier failure — a refusal, a malformed
response, an out-of-vocabulary token — records as a failure of the answer
rather than a PARTIAL verdict, because a verifier that grades its own
uncertainty SUPPORTED is not a verification.

Conditional phrasing is never flattened to unconditional: FR-006 is a property
of fidelity, not of scoring. A PARTIAL verdict is allowed to say "the answer
behaves as if a shared permission were unconditional" — it is not an artifact
to be coerced into SUPPORTED.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from app.generation.provider import LLMProvider

Verdict = Literal["SUPPORTED", "UNSUPPORTED", "PARTIAL"]

_SYSTEM_PROMPT = """You are the answer verifier in a retrieval system.

You are shown the answer a writer produced and the evidence that writer was
allowed to use. Your job: classify whether the answer is grounded in that
evidence.

Return strict JSON: {"classification": "SUPPORTED" | "UNSUPPORTED" | "PARTIAL", "reason": <string>}
- SUPPORTED: every substantive claim of the answer is stated or clearly implied by the evidence; conditional statements carry their conditions.
- UNSUPPORTED: the answer states a fact the evidence does not carry, or answers when the evidence shows the question is unsupported.
- PARTIAL: some claims are supported, but a condition is dropped, a scope is overstated, or one claim is not supported.

Anything outside the three strings is a failure of this response, not a verdict
we will accept."""


@dataclass(frozen=True, slots=True)
class Verification:
    classification: Verdict
    reason: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


async def verify_answer(answer: str, *, evidence_text: str, provider: LLMProvider) -> Verification:
    """Classify the answer against the evidence it was handed."""
    user = f"Answer:\n{answer}\n\nEvidence:\n{evidence_text}"
    outcome: Any = await provider.classify(system=_SYSTEM_PROMPT, user=user)

    # Not strictly a dict by the type's own contract, but the boundary is where
    # malformed output is expected in tests, hence:
    if not isinstance(outcome, dict):
        return Verification(
            classification="UNSUPPORTED", reason="verifier response malformed", raw={}
        )

    classification = outcome.get("classification")
    if classification not in {"SUPPORTED", "UNSUPPORTED", "PARTIAL"}:
        # An unparseable verdict is a failure of the classifier, never a fifth state.
        return Verification(
            classification="UNSUPPORTED",
            reason=f"classifier returned an unknown verdict: {classification!r}",
            raw=outcome,
        )
    reason = str(outcome.get("reason", "")) or None
    return Verification(classification=classification, reason=reason, raw=outcome)


__all__ = ["Verification", "Verdict", "verify_answer"]
