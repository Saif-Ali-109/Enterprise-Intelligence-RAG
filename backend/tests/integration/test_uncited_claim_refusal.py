"""The pipeline refuses an answer with an uncited claim (FR-002, Principle I).

The runtime half of the rule `tests/unit/test_uncited_claims.py` states, and the
other half of the guarantee `fabricated_facts` measures. Without this, the
absolute-zero gate reports fabricated facts about answers the system served on
purpose.

**The retry is asserted as carefully as the refusal**, because it is what makes
the refusal affordable. Measured 2026-10-04, the model marks claims in about half
its answers; refusing the first unmarked answer would throw away roughly half of
all answerable questions. So the pipeline asks again, and refuses only when the
second answer is still unmarked. Both paths are here, with the provider scripted
to produce each.

**Why the fake provider and a real database.** The decision is the pipeline's; it
is observable at the response. What is real here is the ORM write — a refused
question still records a `query_logs` row, and a test that only checked the
response would not notice if the audit trail lost refusals.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
from app.chat.service import run_chat
from app.db.session import dispose_engine, get_session_factory
from sqlalchemy import select

from tests.fixtures.credentials import seed_placeholder_credentials
from tests.fixtures.database import (
    describe_connection_failure,
    migrate_to_head,
    truncate_all,
)

pytestmark = [pytest.mark.integration]

# Placeholder keys, installed once at import. No vendor is contacted: the provider
# is scripted, and the pipeline's decision is the thing under test.
seed_placeholder_credentials()

QUESTION = "How do I restrict who can view or edit a page in Confluence Cloud?"


def _completion(answer: str) -> Any:
    """A provider completion in the shape the generator's own parser accepts."""
    payload = {
        "answerable": True,
        "answer": answer,
        "citations": [
            {
                "evidence_id": "u1",
                "source_url": "https://support.atlassian.com/confluence-cloud/docs/create-a-space/",
                "quote": "Open the Share dialog and choose Restrict.",
            }
        ],
    }
    # The real `Completion` dataclass, not a dict: `assert_no_reasoning` walks the
    # adapter's return value's fields, and a dict would not be what the pipeline
    # normally receives.
    from app.generation.provider import Completion

    return Completion(text=json.dumps(payload), model="scripted")


class ScriptedProvider:
    """A provider that returns the scripted answers in order, then raises.

    Raising rather than repeating the last answer is deliberate: a test that
    cannot distinguish the second attempt from the first cannot tell a retry from
    a loop, and "the retry happened" is the property under test.
    """

    def __init__(self, answers: list[str], *, verify: str = "SUPPORTED") -> None:
        self._answers = list(answers)
        self._verify = verify
        self.calls = 0
        self.verified: list[str] = []

    async def classify(self, **kwargs: Any) -> dict[str, Any]:
        """Serves two callers, distinguished by their system prompt.

        The query analyzer wants a product/category opinion, and this test is about
        what happens *after* evidence is selected — a stub that named a product
        would make the retrieval filters depend on a stub's opinion. The verifier
        wants a verdict, and returning the analyzer's shape there would be a
        malformed response, which the verifier treats as UNSUPPORTED.
        """
        system = str(kwargs.get("system", ""))
        if "answer verifier" in system:
            return {"classification": self._verify, "reason": "scripted"}
        return {
            "detected_product": None,
            "detected_category": None,
            "intent": "factual",
            "entities": [],
            "confidence": {"product": 0.0, "category": 0.0, "intent": 0.0, "entities": 0.0},
        }

    async def complete(self, **kwargs: Any) -> Any:
        if not self._answers:
            raise AssertionError("the provider was called more times than the script allows")
        answer = self._answers.pop(0)
        self.calls += 1
        return _completion(answer)


CITED = "Open the Share dialog and choose Restrict. [1]"

UNCITED = (
    "In Confluence Cloud you add page restrictions to control who can see or edit a page. "
    "To set the restrictions, open the Share dialog on the page and choose Restrict."
)


@pytest.fixture(scope="module")
def _schema() -> None:
    """The schema, as the migrations build it. Sync: `env.py` calls `asyncio.run`
    at import time, so it cannot be invoked from inside a running loop — which an
    async fixture is."""
    try:
        migrate_to_head()
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"{describe_connection_failure()}: {exc}")


@pytest.fixture(autouse=True)
async def _database(_schema: None) -> AsyncIterator[None]:
    """No rows from another test, and the schema still there afterwards."""
    await truncate_all()
    yield
    await truncate_all()
    await dispose_engine()


@pytest.fixture
async def session() -> AsyncIterator[Any]:
    factory = get_session_factory()
    async with factory() as db:
        yield db


async def _seed_corpus(session: Any) -> str:
    """One source, one page, one unit. The minimum that reaches generation."""
    import uuid

    from app.db.models import Document, DocumentUnit, Source

    source_id = uuid.uuid4()
    document_id = uuid.uuid4()
    vector_id = f"{document_id}#0000"

    session.add(
        Source(
            id=source_id,
            name="Confluence Cloud docs",
            start_url="https://example.invalid/",
            allowed_domains=["example.invalid"],
        )
    )
    session.add(
        Document(
            id=document_id,
            source_id=source_id,
            url="https://example.invalid/page",
            canonical_url="https://example.invalid/page",
            title="Page restrictions",
            product="confluence",
            category="page-permissions",
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
            heading_path=["Page restrictions", "Restrict a page"],
            block_types=["heading", "paragraph"],
            token_count=612,
            overlap_tokens=0,
        )
    )
    await session.commit()
    return vector_id


class _SeededStore:
    """Returns one hit, and nothing else.

    Provenance is not supplied here: `retriever.retrieve` joins it from the
    registry, so the document and units below have to exist for the pipeline to
    reach generation at all. Returning a hit id that is not in the database would
    produce an answer with no citable source, and this test would then be
    measuring the wrong refusal.
    """

    def __init__(self, vector_id: str) -> None:
        self._vector_id = vector_id
        self.search_calls = 0

    async def search(self, **kwargs: Any) -> list[Any]:
        from app.retrieval.vector_store import SearchHit

        self.search_calls += 1
        return [SearchHit(id=self._vector_id, score=0.82)]

    async def fetch(self, ids: list[str], **_kwargs: Any) -> list[Any]:
        """Text lives in the vector store, never in the registry (data-model §1).

        The reranker refuses to score a candidate with no text, and the honest
        answer to that is a refusal — so a store that returned nothing here would
        test the reranker's guard instead of the citation contract.
        """
        from app.retrieval.vector_store import VectorRecord

        return [
            VectorRecord(
                id=vector_id,
                text=(
                    "To restrict a page in Confluence Cloud, open the Share dialog and "
                    "choose Restrict. You can then limit Can view or Can edit to specific "
                    "users or groups. Users who need access can request view or edit "
                    "rights, and the content owner or a space admin approves the request."
                ),
            )
            for vector_id in ids
            if vector_id == self._vector_id
        ]


def _store(vector_id: str) -> _SeededStore:
    return _SeededStore(vector_id)


async def test_a_marked_answer_is_served(session: Any) -> None:
    """The control: enforcement must not refuse a properly cited answer."""
    vector_id = await _seed_corpus(session)
    provider = ScriptedProvider([CITED])
    result = await run_chat(
        QUESTION,
        session=session,
        store=_store(vector_id),
        provider=provider,
        inspect=True,
    )

    assert result.payload["outcome"] == "answered"
    assert result.payload["answer"] == CITED
    assert provider.calls == 1


async def test_an_uncited_claim_is_retried_then_refused(session: Any) -> None:
    """Both attempts unmarked: the answer is refused, not served.

    `GENERATION_FAILED` rather than `INSUFFICIENT_EVIDENCE`: the evidence was
    sufficient, the model produced something the contract does not permit, and a
    refusal that blamed the corpus would send an operator to index more pages when
    the problem was the answer.
    """
    vector_id = await _seed_corpus(session)
    provider = ScriptedProvider([UNCITED, UNCITED])
    result = await run_chat(
        QUESTION,
        session=session,
        store=_store(vector_id),
        provider=provider,
        inspect=True,
    )

    payload = result.payload
    assert payload["outcome"] == "refused"
    assert payload["refusal_reason"] == "GENERATION_FAILED"
    assert payload["answer"] is None
    assert payload["citations"] == []
    # Two attempts: the original and the retry. Not one (no retry), not three.
    assert provider.calls == 2


async def test_the_retry_recovers_an_answer_that_marks_its_claims(session: Any) -> None:
    """The case that makes the refusal affordable, and the reason for the design.

    First attempt unmarked, second marked: the answer is served and the user never
    learns there was a retry, because there was nothing for them to do about it.
    """
    vector_id = await _seed_corpus(session)
    provider = ScriptedProvider([UNCITED, CITED])
    result = await run_chat(
        QUESTION,
        session=session,
        store=_store(vector_id),
        provider=provider,
        inspect=True,
    )

    payload = result.payload
    assert payload["outcome"] == "answered"
    assert payload["answer"] == CITED
    assert provider.calls == 2
    assert result.payload["trace"]["generation"]["attempts"] == 2


async def test_a_refusal_is_still_audited(session: Any) -> None:
    """The enforcement did not create an unrecorded outcome.

    Refusals are audit records too: a question refused for an uncited claim is
    exactly the kind of event an operator needs to find afterwards, and a
    response-only test would not notice if the write went missing.
    """
    from app.db.models import QueryLog

    vector_id = await _seed_corpus(session)
    provider = ScriptedProvider([UNCITED, UNCITED])
    await run_chat(
        QUESTION,
        session=session,
        store=_store(vector_id),
        provider=provider,
        inspect=True,
    )

    rows = (
        (await session.execute(select(QueryLog).order_by(QueryLog.created_at.desc()).limit(1)))
        .scalars()
        .all()
    )
    assert rows, "the refused question left no audit row"
    assert rows[0].outcome == "refused"
