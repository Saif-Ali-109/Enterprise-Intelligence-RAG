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
from typing import Any, Literal, Protocol

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
            unit_id=getattr(registry, "unit_id", "") or h.metadata.get("unit_id", "") or "",
            document_id=getattr(registry, "document_id", "")
            or h.metadata.get("document_id", "")
            or "",
            source_id=getattr(registry, "source_id", "") or h.metadata.get("source_id", "") or "",
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


# ============================================================================
# The quality gate and the leads (T101, T104 / FR-007, FR-008)
# ============================================================================

#: Which floor refused, as a code. The prose lives in one place per client and
#: in the refusal view; a reason string here would be parsed by both.
RefusalReason = Literal["BELOW_MIN_RERANK_SCORE", "BELOW_MIN_EVIDENCE_SCORE"]

#: The share of the pool's best score below which a hit is noise rather than a
#: weak lead. See `build_leads` for why this is a fraction and not a constant.
LEAD_FLOOR_FRACTION = 0.25


class _Thresholds(Protocol):
    """The two settings the gate reads. A test can hold its own values."""

    min_rerank_score: float | None
    min_evidence_score: float | None


@dataclass(frozen=True, slots=True)
class QualityDecision:
    """Whether this pool may be answered from at all, and why not.

    `best_score` is reported even when the decision is to refuse: it is what an
    operator needs in order to choose between rephrasing the question and
    lowering the floor.
    """

    refused: bool
    reason: RefusalReason | None
    best_score: float
    weakest_selected: float


def gate_evidence(
    hits: list[RerankedHit],
    *,
    thresholds: _Thresholds,
    selected: list[EvidenceUnit] | None = None,
) -> QualityDecision:
    """Refuse a pool whose evidence cannot support an answer (FR-007).

    Two floors, read from configuration, and neither defaulted to a number:

    * `min_rerank_score` judges the **best** unit in the pool. Not the average
      and not the median: an average lets five irrelevant passages launder one
      relevant one into a passing score, and the answer would then be supported
      by the passage that was not relevant.
    * `min_evidence_score` judges the **weakest unit in the set that would be
      answered from** — the selected set, not the pool. An answer built from
      three strong units and one useless one is an answer with a useless claim
      in it, and the mean of those four hides exactly that.

    An empty pool refuses under both readings, with the rerank floor's reason:
    there is no best score to weigh, and "nothing retrieved" is the retrieval
    floor's failure in the first place.
    """
    best = max((h.rerank_score for h in hits), default=0.0)
    weakest = min((u.rerank_score for u in (selected or [])), default=best)

    rerank_floor = thresholds.min_rerank_score
    if rerank_floor is not None and (not hits or best < rerank_floor):
        return QualityDecision(True, "BELOW_MIN_RERANK_SCORE", best, weakest)

    evidence_floor = thresholds.min_evidence_score
    if evidence_floor is not None and (not selected or weakest < evidence_floor):
        return QualityDecision(True, "BELOW_MIN_EVIDENCE_SCORE", best, weakest)

    return QualityDecision(False, None, best, weakest)


def build_leads(
    hits: list[RerankedHit],
    *,
    limit: int = 3,
    floor: float | None = None,
) -> list[dict[str, Any]]:
    """Weakly related sources, as leads. Never as answers (FR-008).

    Every lead carries `insufficient: True` as a literal, so a client cannot
    render one as support even by accident — and the field is in the contract for
    that reason rather than for decoration.

    `floor` defaults to a fraction of the best score in the pool rather than to a
    constant: what counts as "weakly related" is relative to what the index
    thought was relevant at all, and a fixed number either floods the list on a
    strong question or empties it on a weak one. A hit with no publisher page is
    never offered — a lead the reader cannot check is noise with the shape of a
    URL.
    """
    if not hits or limit <= 0:
        return []

    best = max(h.rerank_score for h in hits)
    threshold = best * LEAD_FLOOR_FRACTION if floor is None else floor

    leads: list[dict[str, Any]] = []
    for hit in sorted(hits, key=lambda h: (-h.rerank_score, h.rank)):
        if len(leads) >= limit:
            break
        if hit.rerank_score < threshold:
            continue
        metadata = hit.metadata or {}
        source_url = str(metadata.get("source_url") or "")
        if not source_url:
            continue
        leads.append(
            {
                "source_url": source_url,
                "title": str(metadata.get("title") or "Untitled page"),
                "heading_path": list(metadata.get("heading_path") or []),
                "relevance": hit.rerank_score,
                "insufficient": True,
            }
        )
    return leads


__all__ = [
    "EvidenceUnit",
    "LEAD_FLOOR_FRACTION",
    "QualityDecision",
    "RefusalReason",
    "build_leads",
    "gate_evidence",
    "select_evidence",
]
