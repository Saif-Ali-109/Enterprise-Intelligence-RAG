"""Grounded generation: prompts.py shape and generator.py retry cap."""

from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.unit


def _evidence():
    from app.retrieval.evidence import EvidenceUnit
    from app.retrieval.reranker import RerankedHit

    hit = RerankedHit(
        id="ev-1",
        text="To rotate a token, create a new one and revoke the old one.",
        retrieval_score=0.6,
        rerank_score=0.7,
        rank=1,
        metadata={
            "source_url": "https://support.atlassian.com/api-tokens",
            "title": "Manage API tokens",
            "product": "jira",
        },
    )
    return [
        EvidenceUnit(
            hit=hit, document_id="d1", source_id="s1", category="api-tokens", page_type="doc"
        )
    ]


class _Fake:
    def __init__(self, texts):
        self._texts = list(texts)
        self.calls = 0

    async def complete(self, *, system, user, model=None, max_tokens=1200, json_mode=False):
        self.calls += 1
        text = self._texts.pop(0) if self._texts else "{}"
        from app.generation.provider import Completion

        return Completion(text=text, model="fake")

    async def classify(self, *, system, user):
        return {}

    async def list_models(self):
        return []


class TestPrompt:
    def test_ids_in_the_prompt_are_the_ids_the_citations_point_at(self) -> None:
        from app.generation.prompts import build_answer_prompt

        system, user = build_answer_prompt("How do I rotate a token?", _evidence())
        assert "### [E1]" in user
        assert "evidence_id: ev-1" in user
        assert "support.atlassian.com/api-tokens" in user
        assert "How do I rotate a token?" in user
        assert "may not" in system.lower() and "invent" in system.lower()

    def test_the_prompt_demands_an_inline_marker_on_every_claim(self) -> None:
        """FR-002 is per claim, so the marker rule is in the prompt, not implied."""
        from app.generation.prompts import build_answer_prompt

        system, _user = build_answer_prompt("How do I rotate a token?", _evidence())
        assert "[E1]" in system
        assert "every substantive claim" in system.lower()

    def test_markers_are_numbered_from_one_in_order(self) -> None:
        from app.generation.prompts import build_answer_prompt, marker_for

        evidence = _evidence()
        second = type(evidence[0])(
            hit=type(evidence[0].hit)(
                id="ev-2",
                text=evidence[0].hit.text,
                retrieval_score=0.4,
                rerank_score=0.4,
                rank=2,
                metadata=evidence[0].hit.metadata,
            ),
            document_id="d2",
            source_id="s1",
            category="api-tokens",
            page_type="doc",
        )
        _system, user = build_answer_prompt("q", [*evidence, second])

        assert user.index("### [E1]") < user.index("### [E2]")
        assert marker_for(1) == "E1"

    def test_refusal_prompt_is_stated(self) -> None:
        from app.generation.prompts import build_answer_prompt

        _system, _user = build_answer_prompt("How do I rotate a token?", _evidence())
        assert "answerable" in _system.lower()
        assert "decline rather than speculate" in _system.lower()

    def test_empty_evidence_is_a_refs_refusal_not_prompt(self) -> None:
        from app.generation.prompts import build_answer_prompt

        with pytest.raises(ValueError):
            build_answer_prompt("q", [])


class TestGenerator:
    @pytest.mark.asyncio
    async def test_malformed_response_retries_to_two(self) -> None:
        from app.generation.generator import generate_answer

        provider = _Fake(["not json", "also not json"])
        from app.core.errors import ProviderError

        with pytest.raises(ProviderError):
            await generate_answer("rotate a token", evidence=_evidence(), provider=provider)
        assert provider.calls == 2  # max_attempts bound

    @pytest.mark.asyncio
    async def test_missing_surface_keys_raise(self) -> None:
        from app.core.errors import ProviderError
        from app.generation.generator import generate_answer

        provider = _Fake([json.dumps({"answer": "x"})])
        with pytest.raises(ProviderError):
            await generate_answer("q", evidence=_evidence(), provider=provider)

    @pytest.mark.asyncio
    async def test_answer_parse_round_trip(self) -> None:
        from app.generation.generator import generate_answer

        provider = _Fake(
            [
                json.dumps(
                    {
                        "answer": "Rotate a token.",
                        "answerable": True,
                        "citations": [
                            {
                                "evidence_id": "ev-1",
                                "source_url": "https://support.atlassian.com/api-tokens",
                                "quote": "To rotate a token…",
                            }
                        ],
                    }
                )
            ]
        )
        out = await generate_answer("rotate a token", evidence=_evidence(), provider=provider)
        assert out.answerable is True
        assert out.citations[0].evidence_id == "ev-1"
