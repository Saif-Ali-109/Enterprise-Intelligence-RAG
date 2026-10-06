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

from app.core.config import DocumentCategory, ProductDomain, QuestionIntent, get_settings
from app.core.errors import ProviderError  # re-exported for the fake providers in tests
from app.generation.provider import LLMProvider
from app.retrieval.ambiguity import resolve_ambiguity

_PRODUCT_VALUES = {m.value for m in ProductDomain}
_INTENT_VALUES = {m.value for m in QuestionIntent}
_CATEGORY_VALUES = {m.value for m in DocumentCategory}
_CATEGORY_LIST = ", ".join(sorted(_CATEGORY_VALUES))

_PROMPT_TEMPLATE = """You classify one documentation question for a retrieval system.

Return strict JSON with exactly this shape:
{
  "product":   "jira" | "confluence" | "jsm" | "developer" | "unknown",
  "category":  one of the categories the indexed corpus actually contains, or "unknown",
  "intent":    "factual" | "how_to" | "troubleshoot" | "comparison" | "explain_concept" | "unknown",
  "entities":  [<short entity strings>],
  "confidence": { "product": <0..1>, "category": <0..1>, "intent": <0..1>, "entities": <0..1> }
}

The only categories you may answer with are these, because a category outside
this list cannot match any indexed page and would narrow the search to nothing:
{categories}

Every confidence is how sure you are of the field. Use "unknown" and a low
confidence when the question does not say — never pick a plausible default."""

#: Substituted rather than `.format`-ed: the prompt body is JSON, and every brace
#: in it is data, not a placeholder.
_PROMPT = _PROMPT_TEMPLATE.replace("{categories}", _CATEGORY_LIST)


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
    #: Every product the question names, and whether it spans more than one
    #: (edge case 13). `ambiguity_note` is the sentence a client shows; a null
    #: here means the question named one product or none.
    named_products: list[str] = field(default_factory=list)
    ambiguity_note: str | None = None


def _in_vocabulary(value: Any, valid: set[str]) -> str | None:
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
        candidate = _in_vocabulary(raw.get("product"), _PRODUCT_VALUES)
        if candidate and candidate != "unknown" and product_conf >= threshold:
            product = candidate

    category: str | None = None
    if isinstance(raw, dict):
        # Same rule as product, and for the same reason: a confident category the
        # corpus cannot contain is a filter that matches nothing. Measured on the
        # first live run — `"api"` at 0.95 confidence against a corpus of
        # `rest-api` — and it refused a question the page answered.
        candidate = _in_vocabulary(raw.get("category"), _CATEGORY_VALUES)
        if candidate and candidate != "unknown" and category_conf >= threshold:
            category = candidate

    intent: str | None = None
    if isinstance(raw, dict):
        candidate = _in_vocabulary(raw.get("intent"), _INTENT_VALUES)
        if candidate and candidate != "unknown" and intent_conf >= threshold:
            intent = candidate

    entities: list[str] = []
    if isinstance(raw, dict):
        raw_entities = raw.get("entities")
        if isinstance(raw_entities, list) and entities_conf >= threshold:
            entities = [e.strip() for e in raw_entities if isinstance(e, str) and e.strip()]

    # Ambiguity is decided from the question's own words, not from the model's
    # confidence: a question that names two products is cross-product even when
    # the classifier is certain about one of them, and suppressing the product
    # filter is what stops the other half going unsearched (edge case 13, FR-009).
    ambiguity = resolve_ambiguity(question, product)

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
        named_products=ambiguity.products,
        ambiguity_note=ambiguity.note,
    )


__all__ = ["QueryAnalysis", "analyze_question", "ProviderError"]
