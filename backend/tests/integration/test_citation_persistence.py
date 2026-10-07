"""Marker accounting and citation persistence — the two defects the first live
evaluation run exposed.

**The marker form.** The generation prompt asks for `[E1]`; the stored answer
carries the resolved rank (`[1]`). A metric that recognised only the prompt form
counted three properly cited sentences as fabricated and failed SC-008's
absolute-zero gate on an answer that was fully marked `[1] [2] [3]`. Both forms
are now recognised, and these tests pin both — the rank form because that is what
is stored, the prompt form because that is what the model is asked for.

**Citation persistence.** `citations` rows were never written by any code path:
the table, the model, and the foreign key existed, and an answered request left
zero of them. An audit record that cannot say which passages were served is a
record of a claim rather than a decision, and the per-question zero-count gates
are only auditable from those rows.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import func, select

from tests.fixtures.database import describe_connection_failure, migrate_to_head, truncate_all

pytestmark = pytest.mark.integration

REQUEST_ID = "44444444-4444-4444-8444-444444444444"
DOCUMENT_ID = uuid.UUID("55555555-5555-4555-8555-555555555555")
UNIT_ID = uuid.UUID("66666666-6666-4666-8666-666666666666")

CITATION = {
    "rank": 1,
    "document_id": str(DOCUMENT_ID),
    "unit_id": str(UNIT_ID),
    "source_url": "https://support.atlassian.com/jira-cloud-administration/docs/",
    "title": "Manage app access",
    "product": "jira",
    "category": "permissions",
    "heading_path": ["App access"],
    "quote": "Only users in an app-access-assigned group can see the page.",
    "retrieval_score": 0.8,
    "rerank_score": 0.91,
    "validation_state": "valid",
    "validation_note": None,
}


# ---------------------------------------------------------------------------
# Marker accounting
# ---------------------------------------------------------------------------


class TestMarkerAccounting:
    """Both marker shapes, because the metric is only honest if it counts a
    claim as cited whichever shape the model chose."""

    def test_rank_markers_before_the_full_stop_are_cited_claims(self) -> None:
        from app.evaluation.metrics import fabricated_facts

        # This is the shape the live answers actually take.
        answer = (
            "Only users with app access can see the page [1]. "
            "A provisioning fault in the identity provider can also hide it [2]."
        )
        assert fabricated_facts(answer) == 0

    def test_a_marker_leading_the_next_sentence_belongs_to_the_previous_claim(self) -> None:
        from app.evaluation.metrics import fabricated_facts, substantive_sentences

        sentences = substantive_sentences("Only users with app access can see the page. [1]")
        assert sentences == ["Only users with app access can see the page. [1]"]
        assert fabricated_facts("Only users with app access can see the page. [1]") == 0

    def test_prompt_form_markers_are_cited_claims(self) -> None:
        from app.evaluation.metrics import fabricated_facts

        assert fabricated_facts("The page is hidden from users without app access [E1].") == 0

    def test_a_trailing_uncited_claim_is_counted_whatever_came_before(self) -> None:
        from app.evaluation.metrics import fabricated_facts

        answer = (
            "Atlassian recommends clearing the browser cache first [1]. "
            "Escalation is handled entirely by the account owner. "
            "Contacting support resolves the issue within one business day."
        )
        assert fabricated_facts(answer) == 2

    def test_citation_completeness_uses_the_same_two_forms(self) -> None:
        from app.evaluation.metrics import citation_completeness

        assert citation_completeness("Only this sentence is cited [1].") == 1.0
        assert citation_completeness("Only this sentence is cited [E1].") == 1.0
        assert citation_completeness("Nothing here is cited at all.") == 0.0


# ---------------------------------------------------------------------------
# Citation persistence
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def _schema() -> None:
    try:
        migrate_to_head()
    except Exception:  # noqa: BLE001
        pytest.skip(describe_connection_failure())


@pytest.fixture(autouse=True)
async def _database(_schema: None) -> AsyncIterator[None]:
    await truncate_all()
    yield
    await truncate_all()
    from app.db.session import dispose_engine

    await dispose_engine()


async def _seed_registry() -> None:
    from app.db.models import Document, DocumentUnit, Source
    from app.db.session import get_session_factory

    async with get_session_factory()() as session:
        source = Source(
            name="S",
            start_url="https://support.atlassian.com/jira-cloud-administration/docs/",
            allowed_domains=["support.atlassian.com"],
        )
        session.add(source)
        await session.flush()
        document = Document(
            id=DOCUMENT_ID,
            source_id=source.id,
            url="https://support.atlassian.com/jira-cloud-administration/docs/page",
            product="jira",
            category="permissions",
            state="indexed",
            vectors_live=True,
        )
        session.add(document)
        await session.flush()
        session.add(
            DocumentUnit(
                id=UNIT_ID,
                document_id=document.id,
                ordinal=0,
                vector_id=f"{document.id}#0000",
                heading_path=["App access"],
                block_types=["paragraph"],
                token_count=700,
                text_fingerprint="fp",
            )
        )
        await session.commit()


class TestCitationPersistence:
    async def test_an_answered_request_persists_one_row_per_citation(self) -> None:
        from app.chat.audit import record_query
        from app.db.models import Citation
        from app.db.session import get_session_factory

        await _seed_registry()
        async with get_session_factory()() as session:
            await record_query(
                session,
                request_id=REQUEST_ID,
                question="Which users can see this page?",
                outcome="answered",
                answer="Users with app access can see it. [1]",
                citations=[CITATION],
            )
        async with get_session_factory()() as session:
            rows = list(
                (
                    await session.execute(
                        select(Citation).where(Citation.query_log_id == uuid.UUID(REQUEST_ID))
                    )
                ).scalars()
            )
        assert len(rows) == 1
        assert rows[0].rank == 1
        assert rows[0].document_id == DOCUMENT_ID
        assert rows[0].validation_state == "valid"
        assert rows[0].quote.startswith("Only users")
        assert rows[0].heading_path == ["App access"]

    async def test_a_refusal_persists_no_citation_rows(self) -> None:
        from app.chat.audit import record_query
        from app.db.models import Citation
        from app.db.session import get_session_factory

        async with get_session_factory()() as session:
            await record_query(
                session,
                request_id="77777777-7777-4777-8777-777777777777",
                question="What is the airspeed velocity of an unladen swallow?",
                outcome="refused",
                refusal_reason="INSUFFICIENT_EVIDENCE",
            )
        async with get_session_factory()() as session:
            total = (await session.execute(select(func.count(Citation.id)))).scalar_one()
        assert total == 0

    async def test_a_bad_citation_cannot_take_the_audit_row_with_it(self) -> None:
        """Losing evidence is a gap; losing the record is a hole.

        A citation whose foreign keys do not resolve must not roll back the
        `query_logs` row — the row is what a complaint is joined to.
        """
        from app.chat.audit import record_query
        from app.db.models import Citation, QueryLog
        from app.db.session import get_session_factory

        request_id = "88888888-8888-4888-8888-888888888888"
        async with get_session_factory()() as session:
            await record_query(
                session,
                request_id=request_id,
                question="Anything",
                outcome="answered",
                answer="Something. [1]",
                # No document_id: the insert cannot satisfy its foreign key.
                citations=[{"rank": 1, "source_url": "https://example.com", "quote": "q"}],
            )
        async with get_session_factory()() as session:
            row = (
                await session.execute(select(QueryLog).where(QueryLog.id == uuid.UUID(request_id)))
            ).scalar_one_or_none()
            citations = (await session.execute(select(func.count(Citation.id)))).scalar_one()
        assert row is not None, "the audit row was lost with its malformed citation"
        assert row.question == "Anything"
        assert citations == 0
