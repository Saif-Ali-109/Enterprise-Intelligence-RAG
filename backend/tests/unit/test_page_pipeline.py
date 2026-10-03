"""The offline page spine: reject, record, or extract.

The composer that is supposed to explain itself to an operator is the one a
test cannot pin: a page whose rejection reason is recorded as indexing noise
or as an honest extraction failure is exactly the audit trail the crawl job
rows will carry. These tests run against the real stage functions.

FR-029, R-009.
"""

from __future__ import annotations

import pytest
from app.ingestion.page_pipeline import IngestionRejected, ingest_page

pytestmark = pytest.mark.unit

REAL_DOC = """
<html><body>
<main>
<h1>Create a space</h1>
<p>Create a space to organize your work in Confluence. A space holds pages that
your whole team can view and edit. Each space has a unique key that is used in
its URLs, and you can set its look and security level when you create it.

Spaces can be archived after the work inside them is complete. Archiving keeps
pages searchable for those who need them while taking the space out of the
active directory, and it can be reversed at any time. Choose a name that
describes the project so other teams can find it.</p>
</main>
</body></html>
"""


def test_real_html_produces_metadata_and_chunks() -> None:
    page = ingest_page(
        REAL_DOC,
        url="https://support.atlassian.com/confluence-cloud/docs/create-a-space/",
        content_type="text/html; charset=utf-8",
    )
    assert page.metadata.title == "Create a space"
    assert page.content_fingerprint
    assert page.chunks.chunks, "a healthy page must produce units"
    assert page.chunks.oversized or page.chunks.chunks, (
        "every cleared page produces chunks or flagged oversized units"
    )


def test_non_html_is_rejected_before_parsing() -> None:
    with pytest.raises(IngestionRejected) as exc:
        ingest_page('{"json": 1}', url="https://example.com/api", content_type="application/json")
    assert exc.value.reason == "non_html_content_type"


def test_stub_page_is_rejected_as_too_small_recorded() -> None:
    with pytest.raises(IngestionRejected) as exc:
        ingest_page(
            "<html><body><p>Hi</p></body></html>",
            url="https://example.com/x",
            content_type="text/html",
        )
    assert exc.value.reason in ("too_small", "extraction_produced_no_blocks", "error_page")


def test_error_page_is_rejected_not_indexed() -> None:
    html = (
        "<html><body><h1>Page not found</h1>"
        "<p>Page not found. The page you are looking for does not exist. Please check the URL and try again. "
        "If you believe this is an error, contact support. Page not found. The page you are looking for.</p>"
        "</body></html>"
    )
    with pytest.raises(IngestionRejected) as exc:
        ingest_page(html, url="https://example.com/missing", content_type="text/html")
    assert exc.value.reason in ("error_page", "too_small", "boilerplate_heavy")


def test_blocker_never_returns_document_with_zero_chunks() -> None:
    html = (
        "<html><body><nav><p>Home</p></nav>"
        "<h1>Nothing useful</h1>"
        "<p>Content is one sentence. Nothing useful.</p>"
        "</body></html>"
    )
    with pytest.raises(IngestionRejected):
        ingest_page(html, url="https://example.com/blank", content_type="text/html")
