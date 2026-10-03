"""Grounded generation (T086 / FR-001, FR-002).

The model's own knowledge is off limits by prompt (T078). The generator's one
responsibility beyond parsing is to see that an `answerable: false` from the
model is preserved as such and never wrapped in citations — an unanswerable
question is a refusal handled by the service, not a partial-success path for
it to rewrite.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.core.errors import ProviderError
from app.generation.prompts import build_answer_prompt, parse_model_answer
from app.generation.provider import LLMProvider
from app.retrieval.evidence import EvidenceUnit


@dataclass(frozen=True, slots=True)
class ModelCitation:
    evidence_id: str
    source_url: str
    quote: str | None = None


@dataclass(frozen=True, slots=True)
class GeneratedAnswer:
    answer: str
    answerable: bool
    citations: list[ModelCitation] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


def _parse(blob: str, *, attempts: int) -> GeneratedAnswer:
    parsed = parse_model_answer(blob)
    if not isinstance(parsed, dict):
        raise ProviderError(f"generation produced no JSON answer object (attempt {attempts})")

    answer = parsed.get("answer")
    answerable = parsed.get("answerable")
    citations = parsed.get("citations", [])

    if not isinstance(answer, str) or not isinstance(answerable, bool):
        raise ProviderError(f"generation returned a malformed answer object (attempt {attempts})")
    if not isinstance(citations, list):
        raise ProviderError(f"generation returned malformed citations (attempt {attempts})")

    parsed_citations: list[ModelCitation] = []
    for item in citations:
        if not isinstance(item, dict):
            raise ProviderError(f"generation returned a non-object citation (attempt {attempts})")
        evidence_id = item.get("evidence_id")
        source_url = item.get("source_url")
        quote = item.get("quote")
        if not isinstance(evidence_id, str) or not evidence_id:
            raise ProviderError(f"generation returned a citation without an evidence_id (attempt {attempts})")
        # Source_url is recorded as asked, but the citation validator (T085) is
        # what *resolves* it via the registry — the generator's job is to
        # refuse to invent. The link itself is checked there, not here.
        parsed_citations.append(ModelCitation(evidence_id=evidence_id, source_url=str(source_url or ""), quote=quote if isinstance(quote, str) else None))

    return GeneratedAnswer(answer=answer, answerable=answerable, citations=parsed_citations, raw=parsed)


async def generate_answer(
    question: str,
    *,
    evidence: list[EvidenceUnit],
    provider: LLMProvider,
    max_attempts: int = 2,
) -> GeneratedAnswer:
    """One grounded generation, retrying on a malformed response only.

    The retry is for contract failures on the model boundary (unparseable JSON,
    missing keys). It is capped at `max_attempts` so a provider stuck returning
    prose never consumes the loop this function did not open; semantic grounding
    is the *verifier's* loop, which asks the same cap question of the answer it
    received. We do not start that loop here, because a prompt that models what
    it should not do is itself evidence a verifier must see.
    """
    if max_attempts < 1:
        raise ValueError("max_attempts must be >= 1")

    system, user = build_answer_prompt(question, evidence)
    last_error: ProviderError | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            completion = await provider.complete(system=system, user=user, json_mode=True)
            return _parse(completion.text, attempts=attempt)
        except ProviderError as exc:
            last_error = exc
            if attempt == max_attempts:
                raise
    # Unreachable, but keeps the type checker and reader honest about the bound.
    raise last_error or ProviderError("generation failed without a recorded attempt")


__all__ = ["GeneratedAnswer", "ModelCitation", "generate_answer"]