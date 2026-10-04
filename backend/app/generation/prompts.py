"""The answer prompt and evidence serialisation (T078 / FR-001, FR-002).

The prompt is the system's only guardrail at the model boundary. It states its
sentinels explicitly: only supplied evidence may be used; citation identifiers
come from the supplied set and may not be invented; and the model must decline
rather than speculate. It does not argue the model into those rules — it tells
the model the deterministic machinery waiting for its output (the citation
validator, the answer verifier, the refusal path), because a boundary is more
reliable when the model knows it is watched.

**Evidence is rendered with structural separators, never folded into prose.**
A marker like `### E1` survives as a copyable token; the ids the model writes
back are the same tokens. This means a citation maps to an evidence id instead
of going through a second interpretation step.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from typing import Any

from app.retrieval.evidence import EvidenceUnit

_RULES = """You are a documentation answer assistant. A user's question is followed
by the evidence retrieved for it. Answer the question using only that
evidence. You may not use knowledge outside it.

Every citation you attach MUST reference an evidence id that was supplied
below, using its exact identifier. Do not invent identifiers, URLs, titles, or
quotes. If the evidence cannot answer the question, decline rather than speculate: say so
and set "answerable" to false. Do not invent.

**Mark every substantive claim inline.** After each claim, on the same line, write
its evidence marker in brackets — `[E1]`, `[E3]` — using the markers from the list
below. A claim with no marker is a claim the reader cannot check, so an answer
whose sentences carry no markers is refused and treated as if it had not been
written. Mark only what the unit actually supports; a marker on a sentence the
unit does not support is worse than no marker at all.

Match conditional phrasing from the source exactly — if the evidence renders a
permission a prerequisite, so must your answer (FR-006). Do not present a
conditional instruction as if it applied unconditionally.

Return strict JSON only, of exactly this shape:
{
  "answer": <string — a concise answer grounded in the evidence>,
  "answerable": <true | false — false when the evidence does not support an answer>,
  "citations": [
    { "evidence_id": <one of the supplied ids>,
      "source_url": <its source_url as supplied>,
      "quote": <the sentence(s) from the evidence that supports the answer> }
  ]
}"""

#: The marker the model writes inline, and the one the response carries after
#: resolution. `E1` is short enough to type inside a sentence and unique within one
#: answer; the reader sees the citation's rank, which is what the citation list is
#: ordered by, so the two name the same thing on screen.
EVIDENCE_MARKER = re.compile(r"\[E(\d+)\]")


def evidence_ids(units: Iterable[EvidenceUnit]) -> list[str]:
    return [u.hit.id for u in units]


def marker_for(index: int) -> str:
    """`E1` for the first unit.

    One-based, because a prompt that offers `[E0]` and a reader who counts from one
    disagree about the same sentence.
    """
    return f"E{index}"


def build_answer_prompt(question: str, units: list[EvidenceUnit]) -> tuple[str, str]:
    """The system prompt and the user message for the generation stage.

    Returns `(system, user)`, matching the `LLMProvider.complete` signature so
    the generation stage has no prompt-shaped rules of its own: the only string
    it passes on is the question. Systemic constraints live here once, where a
    review can read them, not in the generator's call site.
    """
    if not units:
        raise ValueError("a question with no evidence is a refusal, not a generation request")

    lines = [
        "Evidence for the question. Cite these markers inline:",
        "",
    ]
    for number, unit in enumerate(units, start=1):
        lines.append(f"### [{marker_for(number)}]")
        lines.append(f"- evidence_id: {unit.hit.id}")
        lines.append(
            f"- source_url: {unit.hit.metadata.get('source_url') or unit.hit.metadata.get('title') or 'unknown'}"
        )
        lines.append(f"- title: {unit.hit.metadata.get('title', 'unknown')}")
        lines.append(
            f"- heading_path: {' > '.join(unit.heading_path) if unit.heading_path else '(none)'}"
        )
        lines.append(f"- product: {unit.hit.metadata.get('product', 'unknown')}")
        lines.append(f"- retrieved_score: {unit.retrieval_score}")
        lines.append(f"- rerank_score: {unit.rerank_score}")
        lines.append("")
        lines.append(unit.hit.text.strip())
        lines.append("")
    lines.append("Question:")
    lines.append(question)
    lines.append("")
    lines.append(
        f"Write the answer with an inline marker such as [{marker_for(1)}] after every substantive claim."
    )
    return _RULES, "\n".join(lines)


def parse_model_answer(blob: str) -> dict[str, Any] | None:
    """A JSON answer is required; anything else is a generation failure, never
    a silently accepted prose blob. Returns the parsed dict, or None."""
    try:
        parsed = json.loads(blob)
    except (ValueError, TypeError):
        return None
    if isinstance(parsed, dict):
        return parsed
    return None


__all__ = [
    "EVIDENCE_MARKER",
    "build_answer_prompt",
    "evidence_ids",
    "marker_for",
    "parse_model_answer",
]
