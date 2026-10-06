"""Multi-query rewriting (T080, T114 / FR-013, SC-004).

A question with one part is one query. A question that asks two things is two,
and so are a cross-product comparison and a span with a conjunction: one
undifferentiated search would retrieve evidence for the strongest question and
blur the other's.

The rewrite is deterministic and narrow on purpose: it NEVER rephrases what the
user asked, it only splits it. Rephrasing belongs to the user; the system's job
is to search every part of the same question.

**A cross-product question gets one scoped query per domain, and the scope is
written into the query.** `Why can a user see a linked page in Confluence but not
in Jira? [scope: confluence]` is one query per domain the question spans, which
is what `query_analyzed.rewrite_queries` is for: an operator reading the panel
sees exactly what was searched, including the scope tag. The alternative — the
same question issued twice with a filter the query text does not mention —
reports two identical queries and hides the difference that matters.

The tag is also what the retrieval layer reads. It is a closed format, parsed
rather than pattern-matched loosely, and a question the user typed that happens
to end in `[scope: jira]` is honoured rather than rejected: the filter it selects
is a *narrowing* of the user's own words, and the ambiguity note still reports
every product covered.
"""

from __future__ import annotations

import re

from app.retrieval.query_analyzer import QueryAnalysis

_SEP_PATTERN = re.compile(r"\s+\band\b\s+|\s+as\s+well\s+as\s+|\s+or\s+", re.IGNORECASE)
_LISTING_MARKERS = (" vs ", " vs. ", " versus ")

#: The scope tag's two halves, kept separate so the suffix that writes one and
#: the parse that reads it cannot drift apart.
SCOPE_OPEN = "[scope:"
SCOPE_CLOSE = "]"
_SCOPE_RE = re.compile(re.escape(SCOPE_OPEN) + r"\s*([a-z]+)\s*" + re.escape(SCOPE_CLOSE) + r"\s*$")


def scope_tag(product: str) -> str:
    """`" [scope: jira]"` — the suffix that makes a query domain-specific."""
    return f" {SCOPE_OPEN} {product}{SCOPE_CLOSE}"


def scope_of(query: str) -> str | None:
    """The product a scoped query names, or `None` for an unscoped one."""
    match = _SCOPE_RE.search(query.strip().lower())
    return match.group(1) if match else None


def base_question(query: str) -> str:
    """The query with its scope tag removed, for display and for embedding.

    The scope is *not* embedded: repeating a product name in the text of a
    similarity query biases the ranking toward units that merely mention it. The
    narrowing belongs in the metadata filter, where it is exact.
    """
    return _SCOPE_RE.sub("", query.strip()).strip()


def rewrite_queries(question: str, analysis: QueryAnalysis) -> list[str]:
    """One query per distinct ask, ordered by appearance.

    The split is conservative: a question that does not obviously contain two
    pulls is treated as one. A cross-product question that names both products
    in entities but is genuinely one ask (e.g. "are templates shared") is held
    as one — splitting it would search for something no reader asked for.
    Ambiguity about whether something is one ask or two is surfaced in
    `QueryAnalysis` confidences downstream, not resolved here.
    """
    if analysis.ambiguity_note is not None and len(analysis.named_products) > 1:
        # One scoped query per domain the question spans, in the order the
        # question named them (T114 / SC-004). Each carries the user's own words
        # plus a scope tag, so the difference between them is legible rather than
        # hidden in a filter the response never mentions.
        return [f"{question.strip()}{scope_tag(product)}" for product in analysis.named_products]

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


__all__ = [
    "SCOPE_CLOSE",
    "SCOPE_OPEN",
    "base_question",
    "rewrite_queries",
    "scope_of",
    "scope_tag",
]
