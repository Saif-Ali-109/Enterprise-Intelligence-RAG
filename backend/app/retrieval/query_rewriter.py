"""Multi-query rewriting (T080 / FR-013).

A question with one part is one query. A question that asks two things is two,
and so are a cross-product comparison and a span with a conjunction: one
undifferentiated search would retrieve evidence for the strongest question and
blur the other's. The rewrite is deterministic and narrow on purpose: it NEVER
rephrases what the user asked, it only splits it. Rephrasing belongs to the
user; the system's job is to search every part of the same question.
"""

from __future__ import annotations

import re

from app.retrieval.query_analyzer import QueryAnalysis

_SEP_PATTERN = re.compile(r"\s+\band\b\s+|\s+as\s+well\s+as\s+|\s+or\s+", re.IGNORECASE)
_LISTING_MARKERS = (" vs ", " vs. ", " versus ")


def rewrite_queries(question: str, analysis: QueryAnalysis) -> list[str]:
    """One query per distinct ask, ordered by appearance.

    The split is conservative: a question that does not obviously contain two
    pulls is treated as one. A cross-product question that names both products
    in entities but is genuinely one ask (e.g. "are templates shared") is held
    as one — splitting it would search for something no reader asked for.
    Ambiguity about whether something is one ask or two is surfaced in
    `QueryAnalysis` confidences downstream, not resolved here.
    """
    if _has_detection_conjunction_issues(question, analysis):
        # A cross-product "X vs Y" question is two questions, one per side.
        parts = [
            p.strip(" ?.!") for p in re.split(r"\bvs(?:\.|ersus)?\b", question, flags=re.IGNORECASE)
        ]
        parts = [p for p in parts if p]
        if len(parts) == 2:
            return [parts[0] + "?", parts[1] + "?"]

    split = _conservative_split(question)
    if len(split) <= 1:
        return [question.strip()]
    return split


def _conservative_split(question: str) -> list[str]:
    # Split on a conjunction only when it isolates two complete asks (each
    # carrying its own verb signal).
    clauses = _SEP_PATTERN.split(question)
    if len(clauses) <= 1:
        return [question.strip()]
    bound_clauses = [c.strip(" ?.!") for c in clauses if c.strip(" ?.!")]
    # Both halves must name a concrete imperative/surface verb before we call
    # them two asks — otherwise the conjunction is lexical ("rotate a token and
    # revoke it" is one instruction).
    if all(
        re.search(
            r"\b(how|what|why|where|which|when|is|are|can|do|does|update|create|delete|add|remove|filter|search|export|rotate|configure|set|install)\b",
            c,
            re.IGNORECASE,
        )
        for c in bound_clauses
    ):
        return [c + "?" for c in bound_clauses]
    return [question.strip()]


def _has_detection_conjunction_issues(question: str, analysis: QueryAnalysis) -> bool:
    return any(m in question.lower() for m in _LISTING_MARKERS) and len(analysis.entities) >= 2


__all__ = ["rewrite_queries"]
