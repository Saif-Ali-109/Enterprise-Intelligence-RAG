"""An answered question must be audited as having retrieved something (FR-048).

Measured 2026-10-13, from `query_logs`: an **answered** row read
`candidates_retrieved = 0, candidates_reranked = 0, evidence_selected = 0` while
its own inspection trace reported 12 retrieved candidates. A refusal row for the
same corpus, same window, reported 12.

The cause: the answered payload carried no `searched` block at all. `record_query`
reads `searched.get("candidates_retrieved", 0)`, so the successful path audited as
zero retrieval — the one path where an operator is most likely to be asking "what
did this answer use?", and the one where the audit was blank.

The audit trail contradicting itself is worse than its absence: it is believed, and
it is wrong about exactly the requests that worked. These tests pin the agreed
pairing — the audit row and the trace must tell the same story.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
from app.chat.service import run_chat

from tests.fixtures.credentials import seed_placeholder_credentials
from tests.fixtures.database import (
    describe_connection_failure,
    migrate_to_head,
    truncate_all,
)

pytestmark = [pytest.mark.integration]

seed_placeholder_credentials()

QUESTION = "How do I save a filter in Jira Cloud so I can run it again later?"
# Every substantive sentence carries its own marker — which is what the pipeline
# now requires. The first version of this fixture left the opening sentence
# unmarked and the request came back `GENERATION_FAILED`, from the uncited-claim
# enforcement. That is the control working, and it is why this constant is written
# the way a compliant answer must be.
UNIT_TEXT = (
    "To save a filter in Jira Cloud, open the filter manager and choose Save as filter. [1] "
    "A saved filter can be run again by name from the same screen. [1]"
)


@pytest.fixture(scope="module")
def _schema() -> None:
    try:
        migrate_to_head()
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"{describe_connection_failure()}: {exc}")


@pytest.fixture(autouse=True)
async def _database(_schema: None) -> Any:
    await truncate_all()
    yield
    await truncate_all()
    from app.db.session import dispose_engine

    await dispose_engine()


@pytest.fixture
async def session() -> Any:
    from app.db.session import get_session_factory

    factory = get_session_factory()
    async with factory() as db:
        yield db


class _Store:
    def __init__(self, vector_id: str) -> None:
        self._vector_id = vector_id

    async def search(self, **_kwargs: Any) -> list[Any]:
        from app.retrieval.vector_store import SearchHit

        return [SearchHit(id=self._vector_id, score=0.81)]

    async def fetch(self, ids: list[str], **_kwargs: Any) -> list[Any]:
        from app.retrieval.vector_store import VectorRecord

        return [VectorRecord(id=i, text=UNIT_TEXT) for i in ids if i == self._vector_id]


class _AnsweringProvider:
    """Classifies confidently and answers, so the pipeline reaches the audit."""

    async def classify(self, **kwargs: Any) -> dict[str, Any]:
        system = str(kwargs.get("system", ""))
        if "answer verifier" in system:
            return {"classification": "SUPPORTED", "reason": "scripted"}
        return {
            "detected_product": "jira",
            "detected_category": "user-documentation",
            "intent": "how_to",
            "entities": [],
            "confidence": {"product": 0.95, "category": 0.9, "intent": 0.9, "entities": 0.5},
        }

    async def complete(self, **_kwargs: Any) -> Any:
        from app.generation.provider import Completion

        payload = {
            "answerable": True,
            "answer": UNIT_TEXT,
            "citations": [
                {
                    "evidence_id": "u1",
                    "source_url": "https://example.invalid/save-your-search-as-a-filter",
                    "quote": "open the filter manager and choose Save as filter",
                }
            ],
        }
        return Completion(text=json.dumps(payload), model="scripted")


async def _seed(session: Any) -> str:
    from app.db.models import Document, DocumentUnit, Source

    source_id = uuid.uuid4()
    document_id = uuid.uuid4()
    vector_id = f"{document_id}#0000"
    session.add(
        Source(
            id=source_id,
            name="Jira Cloud administration docs",
            start_url="https://example.invalid/",
            allowed_domains=["example.invalid"],
        )
    )
    session.add(
        Document(
            id=document_id,
            source_id=source_id,
            url="https://example.invalid/save-your-search-as-a-filter",
            title="Save your search as a filter",
            product="jira",
            category="user-documentation",
            page_type="documentation",
            language="en",
            state="indexed",
            vectors_live=True,
            unit_count=1,
            content_fingerprint="fp",
        )
    )
    session.add(
        DocumentUnit(
            id=uuid.uuid4(),
            document_id=document_id,
            ordinal=0,
            vector_id=vector_id,
            text_fingerprint="ufp",
            heading_path=["Save your search as a filter"],
            block_types=["heading", "paragraph"],
            token_count=612,
            overlap_tokens=0,
        )
    )
    await session.commit()
    return vector_id


async def _audit_row(session: Any, request_id: str) -> Any:
    from app.db.models import QueryLog
    from sqlalchemy import select

    return (
        await session.execute(select(QueryLog).where(QueryLog.id == uuid.UUID(request_id)))
    ).scalar_one()


async def test_an_answered_question_audits_what_it_retrieved(session: Any) -> None:
    vector_id = await _seed(session)
    result = await run_chat(
        QUESTION,
        session=session,
        store=_Store(vector_id),
        provider=_AnsweringProvider(),
        inspect=True,
    )
    payload = result.payload
    assert payload["outcome"] == "answered", payload.get("refusal_reason")

    searched = payload["searched"]
    assert searched["candidates_retrieved"] >= 1
    assert searched["candidates_reranked"] >= 1

    row = await _audit_row(session, payload["request_id"])
    assert row.candidates_retrieved == searched["candidates_retrieved"]
    assert row.candidates_reranked == searched["candidates_reranked"]
    assert row.evidence_selected == searched["evidence_selected"]
    assert row.evidence_selected >= 1


async def test_the_audit_row_and_the_trace_agree(session: Any) -> None:
    """The contradiction that found this, pinned so it cannot come back.

    The audit columns and the inspection trace are written from the same run and
    describe the same request. If they disagree, one of them is lying, and an
    operator cannot tell which.
    """
    vector_id = await _seed(session)
    result = await run_chat(
        QUESTION,
        session=session,
        store=_Store(vector_id),
        provider=_AnsweringProvider(),
        inspect=True,
    )
    payload = result.payload
    trace = payload["trace"]
    retrieved_in_trace = len(trace["retrieval"]["candidates"])
    reranked_in_trace = len(trace["reranking"]["candidates"])

    row = await _audit_row(session, payload["request_id"])

    assert row.candidates_retrieved == retrieved_in_trace
    assert row.candidates_reranked == reranked_in_trace
    assert row.candidates_retrieved > 0
