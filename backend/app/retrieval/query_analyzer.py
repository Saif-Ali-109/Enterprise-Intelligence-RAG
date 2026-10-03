"""Question analysis — product, category, intent, and entities, with confidence (FR-009, FR-012).

T079. The analyser calls `LLMProvider.classify` — never a route-level handler —
so the interface a test needs is a two-method double (see
`tests/unit/test_query_analyzer.py`), and the chat service composition root is
the only place a real provider is chosen.

Two rules shape the parsing:

1. **A low-confidence guess is not recorded.** The model is asked for a
   confidence per field; when that confidence is below the configured
   threshold, or the field's value is outside the vocabulary, the field is
   surfaced as `None`. An honest `null` in the response beats a confident-looking
   wrong product, because the retrieval layer would treat the latter as an
   instruction to narrow (FR-009, FR-010).
2. **The analyser NEVER invents a value.** There is no fallback heuristic that
   guesses "jira" from the presence of the substring. A malformed or unparseable
   response reads as `None` fields and an empty entity list, which the caller
   can only distinguish from an honest answer, and which makes the failure
   observable rather than silently swapping in a heuristic identity.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.core.config import ProductDomain, QuestionIntent, get_settings
from app.core.errors import ProviderError  # re-exported for the fake providers in tests
from app.generation.provider import LLMProvider

_PRODUCT_VALUES = {m.value for m in ProductDomain}
_INTENT_VALUES = {m.value for m in QuestionIntent}

_PROMPT = """You classify one documentation question for a retrieval system.

Return strict JSON with exactly this shape:
{
  "product":   "jira" | "confluence" | "jsm" | "developer" | "unknown",
  "category":  <short domain string, e.g. "projects", "api-tokens"> | "unknown",
  "intent":    "factual" | "how_to" | "troubleshoot" | "comparison" | "explain_concept" | "unknown",
  "entities":  [<short entity strings>],
  "confidence": { "product": <0..1>, "category": <0..1>, "intent": <0..1>, "entities": <0..1> }
}

Every confidence is how sure you are of the field. Use "unknown" and a low
confidence when the question does not say — never pick a plausible default."""


@dataclass(frozen=True, slots=True)
class QueryAnalysis:
    """What the answer stage needs to know about the question.

    `None` fields are the contract's "unknown"; an unset product/category/intent
    tells the retrieval layer about its low confidence, and the events channel
    renders that field as `null` rather than a guess.
    """

    original_question: str
    detected_product: str | None
    detected_category: str | None
    intent: str | None
    entities: list[str] = field(default_factory=list)
    classification_confidence: dict[str, float] = field(default_factory=dict)


def _ignored_vocabulary(value: Any, valid: set[str]) -> str | None:
    if isinstance(value, str) and value.strip().lower() in valid:
        return value.strip().lower()
    return None


def _bounded_float(value: Any) -> float | None:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if 0.0 <= f <= 1.0:
        return f
    return None


async def analyze_question(
    question: str,
    *,
    provider: LLMProvider | None = None,
) -> QueryAnalysis:
    """Classify one question. Never returns a forced value for an uncertain field.

    `provider` is the `LLMProvider` to call; production code wires it to the
    composition root (`app.chat.service`), and tests pass a fake. It is never
    a direct vendor import.
    """
    if provider is None:
        from app.generation.provider import get_language_model

        provider = get_language_model()

    settings = get_settings()
    raw = await provider.classify(system=_PROMPT, user=question)

    conf_block: Any = raw.get("confidence") if isinstance(raw, dict) else None
    if not isinstance(conf_block, dict):
        conf_block = {}

    def _confidence(name: str) -> float:
        return _bounded_float(conf_block.get(name)) or 0.0

    product_conf = _confidence("product")
    category_conf = _confidence("category")
    intent_conf = _confidence("intent")
    entities_conf = _confidence("entities")

    threshold = settings.classification_confidence_threshold

    product: str | None = None
    if isinstance(raw, dict):
        candidate = _ignored_vocabulary(raw.get("product"), _PRODUCT_VALUES)
        if candidate and candidate != "unknown" and product_conf >= threshold:
            product = candidate

    category: str | None = None
    if isinstance(raw, dict):
        candidate = raw.get("category")
        if isinstance(candidate, str):
            candidate = candidate.strip()
            if candidate and candidate.lower() != "unknown" and category_conf >= threshold:
                category = candidate

    intent: str | None = None
    if isinstance(raw, dict):
        candidate = _ignored_vocabulary(raw.get("intent"), _INTENT_VALUES)
        if candidate and candidate != "unknown" and intent_conf >= threshold:
            intent = candidate

    entities: list[str] = []
    if isinstance(raw, dict):
        raw_entities = raw.get("entities")
        if isinstance(raw_entities, list) and entities_conf >= threshold:
            entities = [e.strip() for e in raw_entities if isinstance(e, str) and e.strip()]

    return QueryAnalysis(
        original_question=question,
        detected_product=product,
        detected_category=category,
        intent=intent,
        entities=entities,
        classification_confidence={
            "product": product_conf,
            "category": category_conf,
            "intent": intent_conf,
            "entities": entities_conf,
        },
    )


__all__ = ["QueryAnalysis", "analyze_question", "ProviderError"]
