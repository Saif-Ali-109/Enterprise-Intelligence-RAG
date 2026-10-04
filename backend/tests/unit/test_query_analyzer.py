"""Question analysis: structured reading of an operator's question (T073 / FR-009, FR-012).

The tests here are offline and use a fake provider: the interesting behaviour
is not what the model says but how the analyser reacts to what the model says —
how it treats low confidence, malformed output, and out-of-vocabulary fields,
and how it refuses to invent one of them.
"""

from __future__ import annotations

from typing import Any

import pytest

pytestmark = pytest.mark.unit


class _FakeProvider:
    """Implements only what `query_analyzer` needs from the `LLMProvider` surface."""

    def __init__(self, payload: dict[str, Any] | BaseException) -> None:
        self.payload = payload
        self.calls: list[dict[str, str]] = []

    async def classify(self, *, system: str, user: str) -> dict[str, Any]:
        self.calls.append({"system": system, "user": user})
        if isinstance(self.payload, BaseException):
            raise self.payload
        return self.payload


class TestAnalysisCompleteness:
    """FR-012: question analysis must yield product, category, intent, entities, and confidences."""

    @pytest.mark.asyncio
    async def test_every_field_reports_a_confidence(self) -> None:
        from app.retrieval.query_analyzer import analyze_question

        result = await analyze_question(
            "How do I rotate an API key in Jira?",
            provider=_FakeProvider(
                {
                    "product": "jira",
                    "category": "administration",
                    "intent": "how_to",
                    "entities": ["API key", "rotation"],
                    "confidence": {
                        "product": 0.94,
                        "category": 0.88,
                        "intent": 0.91,
                        "entities": 0.9,
                    },
                }
            ),
        )

        assert result.detected_product == "jira"
        assert result.detected_category == "administration"
        assert result.intent == "how_to"
        assert result.entities == ["API key", "rotation"]
        assert set(result.classification_confidence) == {
            "product",
            "category",
            "intent",
            "entities",
        }
        for field, value in result.classification_confidence.items():
            assert isinstance(value, float), f"{field} confidence must be a float"

    @pytest.mark.asyncio
    async def test_cross_product_question_is_not_split_by_default(self) -> None:
        from app.retrieval.query_analyzer import analyze_question

        result = await analyze_question(
            "Are sprint templates shared between Jira and Confluence?",
            provider=_FakeProvider(
                {
                    "product": "unknown",
                    "category": "unknown",
                    "intent": "factual",
                    "entities": ["sprint templates", "Jira", "Confluence"],
                    "confidence": {
                        "product": 0.4,
                        "category": 0.3,
                        "intent": 0.9,
                        "entities": 0.95,
                    },
                }
            ),
        )

        # A low-confidence product is reported as unknown — the search must not
        # be narrowed to a guess. FR-009, FR-010.
        assert result.detected_product is None
        assert result.detected_category is None
        assert result.intent == "factual"
        assert "Jira" in result.entities


class TestUnknownIsBetterThanForced:
    """FR-009: never force a classification to fill a field from a low-confidence read."""

    @pytest.mark.asyncio
    async def test_low_confidence_product_maps_to_unknown(self) -> None:
        from app.retrieval.query_analyzer import analyze_question

        result = await analyze_question(
            "how do spaces work",
            provider=_FakeProvider(
                {
                    "product": "confluence",
                    "category": "unknown",
                    "intent": "explain_concept",
                    "entities": ["spaces"],
                    "confidence": {
                        "product": 0.41,
                        "category": 0.2,
                        "intent": 0.92,
                        "entities": 0.7,
                    },
                }
            ),
        )
        # 0.41 < 0.6 threshold: the model's guess is discarded, not honoured.
        assert result.detected_product is None
        assert result.intent == "explain_concept"

    @pytest.mark.asyncio
    async def test_confidence_at_exactly_the_threshold_is_honoured(self) -> None:
        from app.retrieval.query_analyzer import analyze_question

        result = await analyze_question(
            "rotate my tokens",
            provider=_FakeProvider(
                {
                    "product": "jira",
                    "category": "api-tokens",
                    "intent": "how_to",
                    "entities": ["tokens"],
                    "confidence": {"product": 0.6, "category": 0.6, "intent": 0.6, "entities": 0.6},
                }
            ),
        )
        assert result.detected_product == "jira"

    @pytest.mark.asyncio
    async def test_malformed_output_yields_all_unknown(self) -> None:
        from app.retrieval.query_analyzer import analyze_question

        result = await analyze_question(
            "something",
            provider=_FakeProvider(
                {"product": 42, "intent": None, "entities": "not-a-list", "confidence": "high"}
            ),
        )
        assert result.detected_product is None
        assert result.detected_category is None
        assert result.intent is None
        assert result.entities == []

    @pytest.mark.asyncio
    async def test_missing_confidence_block_is_treated_as_low_confidence(self) -> None:
        from app.retrieval.query_analyzer import analyze_question

        result = await analyze_question(
            "what is a sprint",
            provider=_FakeProvider(
                {
                    "product": "jira",
                    "category": "projects",
                    "intent": "explain_concept",
                    "entities": [],
                }
            ),
        )
        # The value is in the output object but the confidence is unknown: treat
        # as unclassified rather than trusting the enum string on its own.
        assert result.detected_product is None
        assert result.intent is None

    @pytest.mark.asyncio
    async def test_a_provider_refusal_propagates_as_provider_error(self) -> None:
        from app.core.errors import ProviderError
        from app.retrieval.query_analyzer import analyze_question

        with pytest.raises(ProviderError):
            await analyze_question(
                "anything",
                provider=_FakeProvider(ProviderError("rate limited")),
            )

    @pytest.mark.asyncio
    async def test_out_of_vocabulary_values_are_garbage_collected_to_unknown(self) -> None:
        from app.retrieval.query_analyzer import analyze_question

        result = await analyze_question(
            "how do spaces work in confluence cloud",
            provider=_FakeProvider(
                {
                    "product": "jira service desk",  # not a configured ProductDomain
                    "category": "confluence",
                    "intent": "factual",
                    "entities": ["spaces"],
                    "confidence": {
                        "product": 0.99,
                        "category": 0.5,
                        "intent": 0.99,
                        "entities": 0.5,
                    },
                }
            ),
        )
        # The enum only has four values; an unrecognised one is recorded as unknown.
        assert result.detected_product is None


class TestCategoryVocabulary:
    """A category the corpus cannot contain must not become a filter.

    Measured on the first live retrieval run rather than anticipated: the
    analyser answered `category: "api"` with 0.95 confidence, the extractor had
    written `rest-api`, the filter matched nothing, and the pipeline refused a
    question the crawled page answered. Confidence was never the issue.
    """

    @pytest.mark.asyncio
    async def test_a_category_outside_the_corpus_vocabulary_is_unknown(self) -> None:
        from app.retrieval.query_analyzer import analyze_question

        result = await analyze_question(
            "How do I create an issue with the Jira Cloud platform REST API?",
            provider=_FakeProvider(
                {
                    "product": "jira",
                    "category": "api",
                    "intent": "how_to",
                    "entities": ["issue"],
                    "confidence": {
                        "product": 0.99,
                        "category": 0.95,
                        "intent": 0.98,
                        "entities": 0.9,
                    },
                }
            ),
        )
        assert result.detected_category is None

    @pytest.mark.asyncio
    async def test_a_category_the_extractor_can_write_is_kept(self) -> None:
        from app.retrieval.query_analyzer import analyze_question

        result = await analyze_question(
            "How do I create an issue?",
            provider=_FakeProvider(
                {
                    "product": "jira",
                    "category": "rest-api",
                    "intent": "how_to",
                    "entities": [],
                    "confidence": {
                        "product": 0.9,
                        "category": 0.9,
                        "intent": 0.9,
                        "entities": 0.0,
                    },
                }
            ),
        )
        assert result.detected_category == "rest-api"

    def test_the_vocabulary_offered_is_the_one_the_extractor_writes(self) -> None:
        """Both sides of the filter must read from one list, or this drifts again."""
        from app.core.config import DocumentCategory
        from app.ingestion.metadata import _CATEGORY_BY_SEGMENT

        corpus_values = set(_CATEGORY_BY_SEGMENT.values())
        assert corpus_values <= {m.value for m in DocumentCategory}

    @pytest.mark.asyncio
    async def test_the_prompt_names_the_vocabulary_it_will_be_held_to(self) -> None:
        from app.core.config import DocumentCategory
        from app.retrieval.query_analyzer import analyze_question

        provider = _FakeProvider(
            {
                "product": "jira",
                "category": "unknown",
                "intent": "unknown",
                "entities": [],
                "confidence": {
                    "product": 0.0,
                    "category": 0.0,
                    "intent": 0.0,
                    "entities": 0.0,
                },
            }
        )
        await analyze_question("q", provider=provider)
        system = provider.calls[0]["system"]
        assert "rest-api" in system
        assert all(m.value in system for m in DocumentCategory)


class TestAnalyserMessage:
    @pytest.mark.asyncio
    async def test_question_is_passed_through_verbatim(self) -> None:
        from app.retrieval.query_analyzer import analyze_question

        provider = _FakeProvider(
            {
                "product": "jira",
                "category": "projects",
                "intent": "how_to",
                "entities": [],
                "confidence": {"product": 0.9, "category": 0.9, "intent": 0.9, "entities": 0.0},
            }
        )
        await analyze_question("How do I create a project?", provider=provider)
        assert provider.calls[0]["user"] == "How do I create a project?"
        assert "product" in provider.calls[0]["system"].lower()
