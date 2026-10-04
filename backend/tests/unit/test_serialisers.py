"""The registry serialisers, against stub rows.

The mappings live in `app/api/serialisers.py` so that no handler can define
`vectors_live` or the crawl counters its own way. That only helps if the mappings
themselves are right in the states the database cannot produce, which is what
this module is for: an ORM row is awkward to construct for "the column is null"
or "the status string is from a newer version", and a stub with the same
attributes is exactly as good for these three functions.

Every other property of these shapes is asserted end to end in
`tests/integration/test_api_sources.py`, against real rows and a real database.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from app.api.serialisers import (
    crawl_job_counters,
    crawl_job_out,
    document_out,
    source_out,
    source_status,
)
from app.schemas.sources import CrawlJobStatus, DocumentState, SourceStatus

pytestmark = pytest.mark.unit

NOW = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)


def _row(defaults: dict[str, Any], **fields: Any) -> SimpleNamespace:
    """A row-shaped object: shared defaults, table defaults, then the test's.

    Three builders rather than one, because `status` means different things in
    the three tables — `active` on a source, `indexed` on a document, `running`
    on a job — and a single stub would have to pick one and quietly assert
    against a value the other two tables do not hold.

    Merged rather than splatted into `_row(**defaults, **fields)` so an override
    cannot collide with a default: `TypeError: got multiple values` on
    `status=...` would have made every status test a fixture bug rather than a
    behaviour assertion.
    """
    base: dict[str, Any] = {
        "id": uuid.uuid4(),
        "source_id": uuid.uuid4(),
        "created_at": NOW,
        "unit_count": 0,
        "requested_at": NOW,
        "started_at": NOW,
        "finished_at": None,
    }
    base.update(defaults)
    base.update(fields)
    return SimpleNamespace(**base)


def source_row(**fields: Any) -> SimpleNamespace:
    return _row(
        {
            "name": "Jira Cloud docs",
            "start_url": "https://support.atlassian.com/jira-software-cloud/docs/",
            "allowed_domains": ["support.atlassian.com"],
            "product_domain": "jira",
            "enabled": True,
            "status": "active",
            "last_crawl_at": None,
        },
        **fields,
    )


def document_row(**fields: Any) -> SimpleNamespace:
    return _row(
        {
            "url": "https://support.atlassian.com/jira-software-cloud/docs/boards/",
            "canonical_url": None,
            "title": "Boards",
            "product": "jira",
            "category": None,
            "page_type": None,
            "language": None,
            "state": "indexed",
            "state_detail": None,
            "vectors_live": False,
            "content_fingerprint": "a" * 64,
            "source_modified_at": None,
            "last_crawled_at": None,
            "indexed_at": None,
            "tombstoned_at": None,
        },
        **fields,
    )


def job_row(**fields: Any) -> SimpleNamespace:
    return _row(
        {
            "target_url": "https://support.atlassian.com/jira-software-cloud/docs/",
            "status": "running",
            "pages_discovered": 0,
            "pages_processed": 0,
            "pages_unchanged": 0,
            "pages_skipped": 0,
            "pages_failed": 0,
            "skipped_reasons": None,
            "error_code": None,
            "error_message": None,
        },
        **fields,
    )


class TestSourceStatus:
    def test_a_disabled_source_reads_as_disabled(self) -> None:
        """Whatever the column says, the operator's instruction is the answer."""
        assert source_status(source_row(enabled=False, status="active")) is SourceStatus.DISABLED
        assert source_status(source_row(enabled=False, status="error")) is SourceStatus.DISABLED

    def test_an_enabled_source_reports_what_the_registry_observed(self) -> None:
        assert source_status(source_row(status="error")) is SourceStatus.ERROR
        assert source_status(source_row(status="active")) is SourceStatus.ACTIVE

    def test_a_status_this_build_does_not_know_is_not_a_500(self) -> None:
        """A value written by a newer version is not a server error.

        `error` rather than `active`, because claiming `active` would be a lie
        about a value this build does not understand; `error` is the only honest
        option left, and the operator can read the row.
        """
        assert source_status(source_row(status="quarantined")) is SourceStatus.ERROR


class TestSourceOut:
    def test_counts_are_carried_and_last_status_is_translated(self) -> None:
        rendered = source_out(
            source_row(), page_count=12, unit_count=57, last_crawl_status="completed_with_errors"
        )

        assert rendered.page_count == 12
        assert rendered.unit_count == 57
        assert rendered.last_crawl_status is CrawlJobStatus.COMPLETED_WITH_ERRORS

    def test_a_source_that_has_never_been_crawled_has_no_status(self) -> None:
        """Null, not `failed`: no crawl is not a failed crawl."""
        rendered = source_out(source_row(), page_count=0, unit_count=0, last_crawl_status=None)

        assert rendered.last_crawl_status is None
        assert rendered.last_crawl_at is None

    def test_allowed_domains_is_copied_not_shared(self) -> None:
        """A caller that mutated the response's list must not mutate the row."""
        row = source_row()
        rendered = source_out(row, page_count=0, unit_count=0)
        rendered.allowed_domains.append("evil.example")

        assert row.allowed_domains == ["support.atlassian.com"]


class TestDocumentOut:
    def test_vectors_live_is_never_derived_from_state(self) -> None:
        """A failed re-crawl leaves prior content live (FR-054).

        `state == "indexed"` would report this document as having nothing
        retrievable, which is the one thing an operator checking whether their
        corpus still answers must not be told.
        """
        rendered = document_out(document_row(state="failed", vectors_live=True), unit_count=4)

        assert rendered.state is DocumentState.FAILED
        assert rendered.vectors_live is True
        assert rendered.unit_count == 4

    def test_state_detail_is_narrowed_to_its_code(self) -> None:
        """The contract types it as a string; the measured detail stays in the log."""
        rendered = document_out(
            document_row(state="failed", state_detail={"code": "extraction_failed", "blocks": 0}),
            unit_count=0,
        )

        assert rendered.state_detail == "extraction_failed"
        assert "blocks" not in rendered.model_dump_json()

    def test_a_detail_that_is_not_an_object_is_not_invented_into_one(self) -> None:
        rendered = document_out(document_row(state="failed", state_detail="boom"), unit_count=0)

        assert rendered.state_detail is None

    def test_a_detail_without_a_code_is_not_invented_into_one(self) -> None:
        rendered = document_out(
            document_row(state="processed", state_detail={"blocks": 3}), unit_count=1
        )

        assert rendered.state_detail is None


class TestCrawlJobSerialisers:
    def test_counters_are_five_and_arithmetic(self) -> None:
        counters = crawl_job_counters(
            job_row(pages_discovered=10, pages_processed=8, pages_unchanged=3, pages_skipped=1)
        )

        assert counters.model_dump() == {
            "discovered": 10,
            "processed": 8,
            "unchanged": 3,
            "skipped": 1,
            "failed": 0,
        }
        # The contract declares exactly these five; a sixth would be a field the
        # contract does not describe, and the suite validates the contract.
        assert set(counters.model_dump()) == set(crawl_job_counters(job_row()).model_dump())

    def test_a_null_counter_reads_as_zero(self) -> None:
        """Defensive against a column added by a migration predating this contract."""
        counters = crawl_job_counters(job_row(pages_discovered=None, pages_processed=None))

        assert counters.discovered == 0
        assert counters.processed == 0

    def test_an_empty_reason_map_is_null_not_an_empty_object(self) -> None:
        """`{}` and absent mean different things; only one of them is "no skips"."""
        assert crawl_job_out(job_row(skipped_reasons={})).skipped_reasons is None
        assert crawl_job_out(job_row(skipped_reasons=None)).skipped_reasons is None
        assert crawl_job_out(job_row(skipped_reasons={"too_small": 2})).skipped_reasons == {
            "too_small": 2
        }

    def test_a_reason_map_of_the_wrong_shape_is_not_passed_through(self) -> None:
        """A list cannot be a count map, and a client would crash on it."""
        assert crawl_job_out(job_row(skipped_reasons=["too_small"])).skipped_reasons is None  # type: ignore[arg-type]

    def test_the_error_message_is_passed_through_unchanged(self) -> None:
        """It was already made safe and bounded by the pipeline; re-editing it here
        would be a second sanitiser with a second set of opinions."""
        rendered = crawl_job_out(
            job_row(status="failed", error_code="INTERNAL_ERROR", error_message="boom")
        )

        assert rendered.error_code == "INTERNAL_ERROR"
        assert rendered.error_message == "boom"
        assert rendered.status is CrawlJobStatus.FAILED
