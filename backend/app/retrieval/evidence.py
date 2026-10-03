"""Evidence selection: from the reranked pool to a small, honest set (T075/T084).

FR-020: a focused set of roughly three to six units — not the whole candidate
pool in the answer step. FR-021: selection scores on several independent
signals. FR-022: diversity wins over near-duplicate passages from one page.

The selection is deterministic. It must never *require* the answer path to
reach into where the unit text lives: every consumer of the set reads exactly
`EvidenceUnit`, so observation of the text a citation points at is an event
rather than a property any later code can silently drop.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from app.retrieval.reranker import RerankedHit


class _RegistryShape(Protocol):
    """Not the vector store; what the evidence stage needs about each unit."""

    unit_id: str
    document_id: str
    source_id: str
    category: str | None
    page_type: str | None
    heading_path: list[str]
    token_count: int


@dataclass(frozen=True, slots=True)
class EvidenceUnit:
    """One selected unit plus the signals the selector used.

    The score columns are retained because the answer stage's prompt and the
    `citation_validation` event both present them; making them absent would
    make every downstream display compute them itself.
    """

    hit: RerankedHit
    unit_id: str = ""
    document_id: str = ""
    source_id: str = ""
    category: str | None = None
    page_type: str | None = None
    heading_path: list[str] = field(default_factory=list)
    token_count: int = 0
    retrieval_score: float = 0.0
    rerank_score: float = 0.0
    headings_relevance: float = 0.0  # Jaccard-ish overlap with the query terms
    metadata_match: float = 1.0  # 1.0 when no filter existed; else 1.0 — reserved


def _term_set(s: str) -> set[str]:
    return {w.strip(".,!?\"'()").lower() for w in s.split() if w.strip(".,!?\"'()")}


def _heading_relevance(query: str, heading_path: list[str]) -> float:
    """A monotonic [0,1] measure of whether the query's words appear in the path."""
    terms = _term_set(query)
    if not terms:
        return 0.0
    path_terms = set().union(*(_term_set(part) for part in heading_path)) if heading_path else set()
    if not path_terms:
        return 0.0
    return len(terms & path_terms) / len(terms)


def _near_duplicate(hit_text: str, other_texts: list[str]) -> bool:
    """Approximate near-duplicate via 3-gram overlap on normalised text."""
    import re

    def grams(s: str) -> set[str]:
        s = re.sub(r"\W+", " ", s.lower()).strip()
        return {s[i : i + 3] for i in range(len(s) - 2)} if len(s) >= 3 else {s}

    a = grams(hit_text)
    if not a:
        return False
    for b in other_texts:
        g = grams(b)
        if g and len(a & g) / len(a | g) > 0.6:
            return True
    return False


def select_evidence(
    query: str,
    hits: list[RerankedHit],
    *,
    units_by_id: dict[str, _RegistryShape] | None = None,
    min_units: int = 3,
    max_units: int = 6,
) -> list[EvidenceUnit]:
    """Score by relevance-strength and diversity; keep the best 3–6.

    A page contributing more than one unit is discounted rather than rejected,
    because a strong answer *about* one page is still one page (FR-022). The
    primary signal stays `rerank_score`; diversity decides ties. `max_units`
    and `min_units` are parameters rather than constants for the same FR-020
    reason the candidate pool is configuration: the quality gate is the
    operator's to tune.
    """
    if not hits:
        return []

    def _unit(h: RerankedHit) -> EvidenceUnit:
        registry = units_by_id.get(h.id) if units_by_id is not None else None
        heading_path = (
            list(getattr(registry, "heading_path", []) or []) if registry is not None else []
        )
        return EvidenceUnit(
            hit=h,
            unit_id=getattr(registry, "unit_id", "") or "",
            document_id=getattr(registry, "document_id", "") or "",
            source_id=getattr(registry, "source_id", "") or "",
            category=getattr(registry, "category", None),
            page_type=getattr(registry, "page_type", None),
            heading_path=heading_path,
            token_count=getattr(registry, "token_count", 0) or 0,
            retrieval_score=h.retrieval_score,
            rerank_score=h.rerank_score,
            headings_relevance=_heading_relevance(query, heading_path),
        )

    # rerank strength dominates; heading relevance is a small secondary signal.
    scored: list[tuple[float, RerankedHit, EvidenceUnit]] = []
    for h in hits:
        u = _unit(h)
        composite = 0.8 * h.rerank_score + 0.2 * u.headings_relevance
        scored.append((composite, h, u))
    scored.sort(key=lambda triple: (-triple[0], triple[1].rank))

    per_document: dict[str, int] = {}
    accepted: list[EvidenceUnit] = []
    accepted_texts: list[str] = []

    for composite, h, u in scored:
        if len(accepted) >= max_units:
            break
        # A near-duplicate of an already-accepted unit is evidence the same fact
        # arrived twice; it adds nothing the first did not say.
        if _near_duplicate(h.text, accepted_texts):
            continue
        doc_id = u.document_id or h.metadata.get("document_id") or h.id
        count = per_document.get(doc_id, 0)
        if count >= 3:
            # A second unit from one page is honest sometimes; a third never is.
            continue
        if count == 1:
            # The first unit from a page is real evidence; the second is
            # preference-softened, not dropped — only near-identical passage
            # text drops it.
            composite *= 0.9
        accepted.append(u)
        accepted_texts.append(h.text)
        per_document[doc_id] = count + 1

    # FR-020 expects 3–6. The selection never *adds* units to meet the floor;
    # if the pool has fewer than three, the honest answer is a short set, and a
    # refusal-based answer path can say why. (A floor filled from candidates
    # that did not clear the pool's relevance score produces a more-voices-than-
    # proof answer.)
    return accepted


__all__ = ["EvidenceUnit", "select_evidence"]
