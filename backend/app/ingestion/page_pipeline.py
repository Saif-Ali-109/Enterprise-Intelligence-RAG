"""The per-page ingestion spine, end to end, offline and deterministic.

This function is the order the stages run in. `fetch`, DNS, robots, and the
vector/database stores are each *before* or *after* this function; inside it,
the only boundary conditions are the judgements — reject, record, or extract —
and those are the ones the crawl job must be able to explain when an operator
asks why a page is absent.

The order matters because it is what makes the rejection reasons an audit
trail instead of a silent absence:

1. **content-type** — decided from the fetch response, before parsing
   (`cleaner.assert_html_content_type`). A non-HTML body can never be
   indexed, and trying to parse one as HTML produces garbage extraction that
   is indistinguishable from a small page.
2. **main-content extraction** — trafilatura can fail or yield nothing; the
   failure is `extraction_failed`, not "a page with no content" — these are
   different facts and the audit trail treats them differently.
3. **boilerplate** — the extracted text must clear the floor and the
   repetition check; a success here means the page is indexing "real
   documentation", which is the whole of FR-029.
4. **chunks** — structure-aware, never crossing headings or code examples.

Fingerprints are computed once, at the end, from extracted text: they are
an output of the same pipeline, not an input to a different one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.ingestion.chunker import ChunkResult, chunk_units
from app.ingestion.cleaner import (
    ContentRejected,
    assert_html_content_type,
    assess_boilerplate,
)
from app.ingestion.extractor import extract_units
from app.ingestion.fingerprint import content_fingerprint
from app.ingestion.metadata import PageMetadata, extract_metadata


class IngestionRejected(Exception):
    """A page refused by the pipeline; the reason is the whole message.

    Kept out of the 14-code `errors.py` vocabulary on purpose: the crawl
    orchestrator records this against `crawl_jobs.state_detail` rather than
    returning it to an HTTP client, and inventing a synthetic error code for
    a per-page outcome would force an API-shaped fact through a domain-shaped
    contract.
    """

    def __init__(self, reason: str, *, details: dict[str, Any] | None = None) -> None:
        self.reason = reason
        self.details = details or {}
        super().__init__(reason)


@dataclass(frozen=True, slots=True)
class PageIngestion:
    url: str
    metadata: PageMetadata
    chunks: ChunkResult
    content_fingerprint: str
    extracted_text: str = ""


def ingest_page(
    html: str,
    *,
    url: str,
    content_type: str | None = None,
) -> PageIngestion:
    """Process one fetched HTML body into metadata, units, fingerprints.

    Never returns a document with zero useful content: a page that clears
    the content-type check, yields extractable main content, and fails the
    boilerplate/size floor is `IngestionRejected`, not an indexed noise box.
    """
    try:
        assert_html_content_type(content_type)
    except ContentRejected as exc:
        raise IngestionRejected(exc.reason, details=exc.details) from exc

    try:
        blocks = extract_units(html, base_url=url)
    except Exception as exc:  # noqa: BLE001 - a parsing failure is a page-level fact
        raise IngestionRejected("extraction_failed", details={"type": type(exc).__name__}) from exc

    if not blocks:
        raise IngestionRejected("extraction_produced_no_blocks")

    text = "\n\n".join(b.text for b in blocks)
    try:
        assess_boilerplate(text)
    except ContentRejected as exc:
        raise IngestionRejected(exc.reason, details=exc.details) from exc

    chunks = chunk_units(blocks)
    metadata = extract_metadata(html, url=url, extracted_text=text)
    return PageIngestion(
        url=url,
        metadata=metadata,
        chunks=chunks,
        content_fingerprint=content_fingerprint(text),
        extracted_text=text,
    )


__all__ = ["IngestionRejected", "PageIngestion", "ingest_page"]
