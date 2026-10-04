"""Citation IDs minted by code, never by model (T085 / FR-003, FR-004, FR-005).

The model's job is text. The identifier a reader clicks on is minted from the
served evidence ids, the link is resolved through the registry/record the
pipeline chose, and the model may only point *at* an id — it never invents one.
An id the model emits that the system did not serve is `stripped`; a link that
disagrees with the record is `repaired`, because the alternative — passing the
model's link through — would silently ship a fabricated reference.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Literal, get_args

ValidationState = Literal["valid", "stripped", "repaired"]


@dataclass(frozen=True, slots=True)
class EvidenceSource:
    """One served unit, in citation-building shape. The quote and scores are
    optional here: the validator does not need them, but carrying them is what
    lets a validated citation be emitted without a second registry read."""

    evidence_id: str
    document_id: str
    unit_id: str
    source_url: str
    title: str
    product: str | None = None
    category: str | None = None
    heading_path: list[str] = field(default_factory=list)
    quote: str | None = None
    retrieval_score: float | None = None
    rerank_score: float | None = None


@dataclass(frozen=True, slots=True)
class ValidatedCitation:
    evidence_id: str
    document_id: str
    unit_id: str
    source_url: str
    title: str
    product: str | None
    category: str | None
    heading_path: list[str]
    quote: str | None
    retrieval_score: float | None
    rerank_score: float | None
    validation_state: ValidationState
    validation_note: str | None = None


@dataclass(frozen=True, slots=True)
class CitationValidation:
    citations: list[ValidatedCitation]
    stripped: int
    repaired: int
    rejected_identifiers: list[str]


def validate_citations(
    cited_ids: list[str],
    *,
    retrieved: list[EvidenceSource],
    cited_links: list[dict[str, Any]] | None = None,
) -> CitationValidation:
    """Mint every citation deterministically from the served evidence set.

    Model output influences *which* citations appear (the cited ids), never the
    link, title, or heading path that a reader lands on. An unknown identifier,
    a duplicated one, or a link that disagrees with the record of truth is
    handled in exactly one of three ways — rejected consequence off the answer,
    or repaired from the record — and the outcome is counted for the
    `citation_validation` event.
    """
    by_id = {s.evidence_id: s for s in retrieved}
    links_by_id: dict[str, str] = {}
    if cited_links:
        for c in cited_links:
            if isinstance(c, dict) and isinstance(c.get("evidence_id"), str):
                links_by_id[c["evidence_id"]] = str(c.get("source_url", ""))

    citations: list[ValidatedCitation] = []
    rejected: list[str] = []
    repaired = 0
    stripped_count = 0
    seen: Counter[str] = Counter()

    for raw_id in cited_ids:
        seen[raw_id] += 1
        if seen[raw_id] > 1:
            # A second mention of the same citation is the first citation again.
            continue
        if raw_id not in by_id:
            rejected.append(raw_id)
            stripped_count += 1
            continue
        source = by_id[raw_id]
        link = links_by_id.get(raw_id, "")
        if link and link != source.source_url:
            repaired += 1
            state: ValidationState = "repaired"
            note: str | None = "model link replaced by registry source_url"
        elif link == "":
            repaired += 1
            state = "repaired"
            note = "model emitted no source_url; registry value used"
        else:
            state = "valid"
            note = None
        citations.append(
            ValidatedCitation(
                evidence_id=source.evidence_id,
                document_id=source.document_id,
                unit_id=source.unit_id,
                source_url=source.source_url,
                title=source.title,
                product=source.product,
                category=source.category,
                heading_path=list(source.heading_path),
                quote=source.quote,
                retrieval_score=source.retrieval_score,
                rerank_score=source.rerank_score,
                validation_state=state,
                validation_note=note,
            )
        )
    return CitationValidation(
        citations=citations,
        stripped=stripped_count,
        repaired=repaired,
        rejected_identifiers=rejected,
    )


__all__ = [
    "CitationValidation",
    "EvidenceSource",
    "ValidatedCitation",
    "ValidationState",
    "validate_citations",
    "get_args",
]
