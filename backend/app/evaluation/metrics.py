"""Metric definitions (T141–T143), all deterministic and all traceable.

**Topic coverage, never URL or string equality for retrieval.** A retrieved
unit *covers* the expected target when it covers the expected product, the
expected category, and the expected heading path — regardless of which URL it
was fetched from (FR-040, FR-063, FR-064). The corpus is ingested by URL from
documentation the publisher reorganises: a URL-pinned grade would report a
retrieval regression the first time a heading is renamed.

**Grades compose on the candidates' registry metadata, not on a second
embedding pass.** Candidates already carry product, category and heading path
from the registry hydration in `retriever.retrieve`; the grade reads what is
already there rather than re-deriving it.

**`None` means not applicable, never "not measured".** For an unsupported
question the right answer is a refusal; a retrieval recall of 0.0 recorded
there would average a correct refusal into a retrieval failure, and a fabricated
0.0 would be a number that never happened (Principle VI).
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# Topic-coverage primitives (T138)
# ---------------------------------------------------------------------------

_TOKEN_SPLIT = re.compile(r"[^a-z0-9]+")
_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "the",
        "is",
        "are",
        "and",
        "or",
        "of",
        "to",
        "in",
        "for",
        "so",
        "by",
        "on",
        "it",
        "be",
        "as",
    }
)


def content_tokens(text: str) -> list[str]:
    return [t for t in _TOKEN_SPLIT.split(text.lower()) if t and t not in _STOPWORDS]


def normalise(text: str) -> str:
    return " ".join(content_tokens(text))


def _normalise_heading(text: str) -> str:
    """Headings keep stopwords: 'A' and 'The' are real path segments."""
    return " ".join(t for t in _TOKEN_SPLIT.split(text.lower()) if t)


def heading_covers(expected: list[str], actual: list[str]) -> bool:
    """The expected path is covered when it appears in order in the actual path.

    Contiguity is not required — an expected two-hop path `["A", "C"]` is
    covered by `["A", "B", "C"]` — but order is, because `["C", "A"]` is a
    different page. Comparison is normalised, so case and punctuation cannot
    move the grade.
    """
    if not expected:
        return False
    exp = [_normalise_heading(part) for part in expected if _normalise_heading(part)]
    act = [_normalise_heading(part) for part in actual if _normalise_heading(part)]
    if not exp:
        return False
    i = 0
    for a in act:
        if i < len(exp) and a == exp[i]:
            i += 1
    return i == len(exp)


def unit_covers_target(
    *,
    product: str | None,
    category: str | None,
    heading_path: list[str],
    expected_product: str | None,
    expected_category: str | None,
    expected_heading_path: list[str],
) -> bool:
    """A unit covers the question's retrieval target (T138)."""
    if expected_product is not None:
        if product is None or normalise(product) != normalise(expected_product):
            return False
    if expected_category is not None:
        if category is None or normalise(category) != normalise(expected_category):
            return False
    if expected_heading_path:
        if not heading_covers(expected_heading_path, heading_path):
            return False
    return True


def topic_covered_in_text(topic: str, text: str) -> bool:
    """An expected topic is covered when its content words are in the text."""
    topic_tokens = content_tokens(topic)
    if not topic_tokens:
        return False
    text_tokens = set(content_tokens(text))
    return all(t in text_tokens for t in topic_tokens)


# ---------------------------------------------------------------------------
# Retrieval metrics (T141)
# ---------------------------------------------------------------------------


def _hit(c: dict[str, Any], expected: Any) -> bool:
    return unit_covers_target(
        product=c.get("product"),
        category=c.get("category"),
        heading_path=list(c.get("heading_path") or []),
        expected_product=getattr(expected, "product", None),
        expected_category=getattr(expected, "category", None),
        expected_heading_path=list(getattr(expected, "heading_path", []) or []),
    )


def retrieval_grades(
    ranked: list[dict[str, Any]], *, expected: Any, is_unsupported: bool
) -> tuple[float | None, float | None, float | None]:
    """recall@5, precision@5, MRR against the topic target. `None` when N/A."""
    if is_unsupported:
        return None, None, None
    top = ranked[:5]
    if not ranked:
        return 0.0, 0.0, 0.0
    hits = [_hit(c, expected) for c in top]
    # A single target per question, so recall is binary: was the target reached.
    recall = 1.0 if any(hits) else 0.0
    precision = sum(hits) / len(top) if top else 0.0
    first = next((i for i, h in enumerate(hits) if h), None)
    mrr = 1.0 / (first + 1) if first is not None else 0.0
    return recall, precision, mrr


# ---------------------------------------------------------------------------
# Answer-quality metrics (T142)
# ---------------------------------------------------------------------------

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")


def substantive_sentences(answer: str) -> list[str]:
    """Sentences that make claims. List sentences and pure caveats are not claims.

    A marker that follows its own sentence's full stop ("... reuse the name.
    [E1]") is reattached to that sentence before judging; otherwise the claim
    would be scored as fabricated because the splitter severed it from its
    evidence.
    """
    sentences: list[str] = []
    for s in _SENTENCE_SPLIT.split(answer.strip()):
        if s.strip().startswith("[E") and sentences:
            sentences[-1] = sentences[-1] + " " + s.strip()
            continue
        tokens = content_tokens(s)
        if len(tokens) >= 4:
            sentences.append(s)
    return sentences


def citation_validity(validation_counts: dict[str, int]) -> float | None:
    """valid / (valid + stripped + rejected-identifiers), null when none issued."""
    valid = validation_counts.get("valid", 0)
    stripped = validation_counts.get("stripped", 0)
    rejected = validation_counts.get("rejected_identifiers", 0)
    total = valid + stripped + rejected
    if total == 0:
        return None
    return valid / total


def claim_coverage(expected_topics: list[str], answer: str) -> float | None:
    """Fraction of expected topics the answer text covers. None when it cannot be judged."""
    if not expected_topics:
        return None
    covered = sum(1 for t in expected_topics if topic_covered_in_text(t, answer))
    return covered / len(expected_topics)


def faithfulness(answer: str, quotes: list[str]) -> float | None:
    """Fraction of substantive answer sentences grounded in a cited quote.

    "Grounded in" is content-word overlap: a claim is faithful when at least
    half of its content words appear in at least one cited quote. Deterministic,
    falsifiable, and sensitive to the one failure FR-002 exists to prevent — an
    answer that names sources without leaning on them.
    """
    claims = substantive_sentences(answer)
    if not claims:
        return None
    quote_tokens: set[str] = set()
    for q in quotes:
        quote_tokens.update(content_tokens(q))
    grounded = 0
    for claim in claims:
        ct = set(content_tokens(claim))
        if ct and len(ct & quote_tokens) >= max(1, math.ceil(len(ct) / 2)):
            grounded += 1
    return grounded / len(claims)


def citation_completeness(answer: str) -> float:
    """Fraction of substantive sentences that carry an inline `[E#]` marker.

    FR-002 makes a claim without a citation a defect; this is the ratio the
    grade reduces to. It is a count of the answer's own sentences, so it is
    never null — zero sentences is zero completeness, and no answer text means
    the question was refused, which the runner handles separately.
    """
    claims = substantive_sentences(answer)
    if not claims:
        return 0.0
    marked = sum(1 for s in claims if re.search(r"\[E\d+\]", s))
    return marked / len(claims)


def fabricated_facts(answer: str) -> int:
    """Substantive sentences with no citation marker — a count, never a ratio (FR-066)."""
    return sum(1 for s in substantive_sentences(answer) if not re.search(r"\[E\d+\]", s))


def answer_relevance(question: str, answer: str) -> float | None:
    """Question content words found in the answer. A relevance floor, not a quality score."""
    q_tokens = set(content_tokens(question))
    if not q_tokens:
        return None
    a_tokens = set(content_tokens(answer))
    if not a_tokens:
        return 0.0
    return len(q_tokens & a_tokens) / len(q_tokens)


# ---------------------------------------------------------------------------
# Aggregate helpers (T143)
# ---------------------------------------------------------------------------


def mean(values: list[float | None]) -> float | None:
    present = [v for v in values if v is not None]
    return sum(present) / len(present) if present else None


def percentile(values: list[int], pct: float) -> int | None:
    """Nearest-rank percentile. Honest about small samples: n=1 → that value."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(pct / 100 * len(ordered)))
    return ordered[min(rank, len(ordered)) - 1]


@dataclass
class PerformanceStats:
    latencies_ms: list[int] = field(default_factory=list)
    errors: int = 0
    total: int = 0

    def p50(self) -> int | None:
        return percentile(self.latencies_ms, 50)

    def p95(self) -> int | None:
        return percentile(self.latencies_ms, 95)

    def error_rate(self) -> float | None:
        if self.total == 0:
            return None
        return self.errors / self.total
