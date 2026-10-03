"""The chat schema surface closes exactly where the contract closes it (T089)."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


class TestAskRequest:
    def test_question_is_bounded(self) -> None:
        from app.schemas.chat import AskRequest
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            AskRequest(question="x" * 2001)
        AskRequest(question="x")

    def test_extra_keys_are_refused(self) -> None:
        from app.schemas.chat import AskRequest
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            AskRequest.model_validate({"question": "q", "token": "A1b2C3"})

    def test_history_is_capped(self) -> None:
        from app.schemas.chat import AskRequest
        from pydantic import ValidationError

        AskRequest(question="q", history=[{"role": "user", "content": "x"}] * 10)
        with pytest.raises(ValidationError):
            AskRequest(question="q", history=[{"role": "user", "content": "x"}] * 11)


class TestAskResponseConformance:
    def test_the_refusal_shape_matches_the_contract(self) -> None:
        from app.schemas.chat import AskResponse

        payload = {
            "request_id": "0f3c1d2a-6b4e-4c1f-9a77-2b8e5d0c4a11",
            "question": "q",
            "outcome": "refused",
            "answer": None,
            "refusal_reason": "INSUFFICIENT_EVIDENCE",
            "searched": {
                "queries": ["q"],
                "products": [None],
                "applied_filters": {},
                "candidates_retrieved": 0,
                "candidates_reranked": 0,
                "evidence_selected": 0,
            },
            "leads": [],
            "citations": [],
            "timing": {"total_ms": 12, "stages_ms": {"retrieve_ms": 3}},
        }
        AskResponse.model_validate(payload)

    def test_the_answered_shape_carries_validated_citations(self) -> None:
        from app.schemas.chat import AskResponse

        payload = {
            "request_id": "0f3c1d2a-6b4e-4c1f-9a77-2b8e5d0c4a11",
            "question": "q",
            "outcome": "answered",
            "answer": "Rotate the token.",
            "citations": [
                {
                    "rank": 1,
                    "document_id": "3b1f0000-0000-0000-0000-000000000001",
                    "unit_id": "9c4e0000-0000-0000-0000-000000000001",
                    "source_url": "https://support.atlassian.com/api-tokens",
                    "title": "Manage API tokens",
                    "product": "jira",
                    "category": "api-tokens",
                    "heading_path": ["Manage API tokens"],
                    "quote": "To rotate…",
                    "retrieval_score": 0.7,
                    "rerank_score": 0.8,
                    "validation_state": "valid",
                }
            ],
            "timing": {"total_ms": 3},
        }
        AskResponse.model_validate(payload)

    def test_an_extra_field_is_a_closed_contract_error(self) -> None:
        from app.schemas.chat import AskResponse
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            AskResponse.model_validate(
                {
                    "request_id": "x",
                    "question": "q",
                    "outcome": "answered",
                    "answer": None,
                    "citations": [],
                    "timing": {"total_ms": 1},
                    "fabricated_field": "nope",
                }
            )
