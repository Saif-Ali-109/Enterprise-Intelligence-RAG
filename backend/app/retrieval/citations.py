"""Citation IDs minted by code, never by model (T085 / FR-003, FR-004, FR-005).

The model's job is text. The identifier a reader clicks on is minted from the
served evidence ids, the link is resolved through the registry/record the
pipeline chose, and the model may only point *at* an id — it never invents one.
An id the model emits that the system did not serve is `stripped`; a link that
disagrees with the record is `repaired`, because the alternative — passing the
model's link through — would silently ship a fabricated reference.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Literal

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


@dataclass(frozen=True, slots=True)
class ResolvedMarkers:
    """The answer text with its markers resolved to citation ranks, and what it cited.

    `text` keeps the markers as `[1]`, `[3]` — the numbers the citation list is
    ordered by, so a reader matches a claim to a source by looking rather than by
    counting. `cited_ids` are the *served* ids the markers pointed at, in the
    order they first appear; a marker naming a number that was never served
    resolves to nothing and is counted in `rejected`.

    An empty `cited_ids` is the load-bearing case: the answer made claims and
    pointed at no evidence, which FR-002 does not permit reaching a client.
    """

    text: str
    cited_ids: list[str]
    rejected: list[str]


def resolve_answer_markers(answer: str, served: list[str]) -> ResolvedMarkers:
    """Resolve `[E1]`-style markers against the units that were actually served.

    Deterministic, and it never asks the model whether its own markers were right
    — the same rule as the citation validator (FR-005). The numbering is
    positional because that is how the prompt presented the evidence; a marker for
    a number that was not served resolves to nothing and is counted, rather than
    being pointed at whatever happens to sit at that index.
    """
    by_marker = {f"E{number}": served[number - 1] for number in range(1, len(served) + 1)}

    cited: list[str] = []
    rejected: list[str] = []
    rank_by_id: dict[str, int] = {}

    def substitute(match: re.Match[str]) -> str:
        marker = f"E{match.group(1)}"
        served_id = by_marker.get(marker)
        if served_id is None:
            rejected.append(marker)
            return ""
        if served_id not in rank_by_id:
            rank_by_id[served_id] = len(cited) + 1
            cited.append(served_id)
        return f"[{rank_by_id[served_id]}]"

    text = re.sub(r"\[E(\d+)\]", substitute, answer)
    # An unresolved marker leaves the gap where it was; collapse the run of spaces
    # it leaves rather than carrying the punctuation damage into the answer text.
    text = re.sub(r"[ \t]{2,}", " ", text).strip()
    return ResolvedMarkers(text=text, cited_ids=cited, rejected=rejected)


#: How precisely the answer's claims were tied to its evidence.
#:
#: `per_claim` — the answer marked each substantive claim inline, and each marker
#: was resolved to the citation that supports it. The strongest form of FR-002.
#:
#: `list` — the answer named its sources but did not mark individual claims, so
#: every claim is covered by a citation list and no claim names its own source.
#: Weaker, and recorded as weaker rather than presented as the same thing.
#:
#: `none` — the answer cited nothing at all. This is the only case refused.
#:
#: Measured 2026-10-04, live: with a prompt that *demands* inline markers, the
#: model produced them in 3 of 6 answers. Refusing the other three would have
#: refused half of all answerable questions over a formatting habit — while their
#: citations resolved exactly as well. So the guarantee graded here is "no claim
#: reaches a client uncited", and the per-claim refinement is a recorded
#: difference in quality rather than a difference in whether the answer is
#: admissible.
CitationGranularity = Literal["per_claim", "list", "none"]


def citation_granularity(markers: ResolvedMarkers, cited_ids: list[str]) -> CitationGranularity:
    """Which of the three states this answer is in. A pure decision."""
    if markers.cited_ids:
        return "per_claim"
    if cited_ids:
        return "list"
    return "none"


__all__ = [
    "CitationGranularity",
    "CitationValidation",
    "EvidenceSource",
    "ResolvedMarkers",
    "ValidatedCitation",
    "ValidationState",
    "citation_granularity",
    "resolve_answer_markers",
    "validate_citations",
]
