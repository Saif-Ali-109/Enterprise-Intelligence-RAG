"""What counts as a claim, and whether it carries a citation.

One definition, used by both the runtime and the evaluation. They existed
separately and had to agree — the runtime decides whether an answer is admissible,
the evaluation decides whether the answer was admissible — and two definitions of
"a sentence with no marker" is one more than a system like this should have. The
metric moved here; the runtime now imports it.

**The rule.** A substantive sentence with no citation marker is a claim a reader
cannot check. Measured on a live answer (2026-10-09), an answer of five
substantive sentences cited three of them and left this one uncited:

    "To set the restrictions, open the Share dialog on the page, choose 'Restrict'
     and select whether you want to limit 'Can view' or 'Can edit' to specific
     users or groups."

That is the most useful sentence in the answer and nothing stopped it reaching a
client. The prompt already promised to refuse such an answer; this module is the
predicate that promise was missing.

**Two marker forms, because the system produces two.** The prompt asks for `[E1]`;
`resolve_answer_markers` rewrites those to the rank form `[1]` in the stored
answer. Recognising only the prompt form reports every correctly cited sentence as
uncited — which it did, on the first live evaluation run, before the regex was
widened.
"""

from __future__ import annotations

import re

__all__ = [
    "MARKER",
    "has_marker",
    "substantive_sentences",
    "unmarked_claims",
]

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")

#: A citation marker in either of the two forms this system produces.
MARKER = re.compile(r"\[(?:E)?\d+\]")

#: Fewer content words than this and it is not a claim: a lead-in ("Note:"), a
#: heading, or a bare marker must never be graded as an uncited fact.
_MIN_CONTENT_WORDS = 4

_WORD_SPLIT = re.compile(r"[a-z0-9]+")

#: Words that carry no claim. Small and deliberate: this is a floor for "is this
#: sentence worth grading", not a bag-of-words relevance model.
_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "but",
        "by",
        "for",
        "from",
        "has",
        "have",
        "in",
        "is",
        "it",
        "its",
        "of",
        "on",
        "or",
        "that",
        "the",
        "this",
        "to",
        "was",
        "were",
        "which",
        "will",
        "with",
    }
)


def has_marker(sentence: str) -> bool:
    """Whether this sentence carries a citation marker."""
    return MARKER.search(sentence) is not None


def _content_words(text: str) -> list[str]:
    return [t for t in _WORD_SPLIT.findall(text.lower()) if t not in _STOPWORDS]


def substantive_sentences(answer: str) -> list[str]:
    """The answer's claims, one per sentence, each with its citation markers.

    Two marker placements occur in practice and both are handled, because the
    check is only honest if it treats a claim as cited whichever shape the model
    chose: `[1].` (marker before the full stop — what the live answers look like)
    and `. [1] Claim two.` (marker leading the next sentence). In the second shape
    the marker belongs to the claim it *follows*, so it is moved back onto that
    sentence, and the text after it becomes the next claim — which, having no
    marker of its own, is correctly uncited.
    """
    sentences: list[str] = []
    for segment in (s.strip() for s in _SENTENCE_SPLIT.split(answer.strip())):
        if not segment:
            continue
        prefix = ""
        remainder = segment
        while True:
            match = MARKER.match(remainder)
            if match is None:
                break
            prefix += f"{match.group(0)} "
            remainder = remainder[match.end() :].strip()
        if prefix and sentences:
            sentences[-1] = f"{sentences[-1]} {prefix.strip()}"
        if not remainder or len(_content_words(remainder)) < _MIN_CONTENT_WORDS:
            continue
        sentences.append(remainder)
    return sentences


def unmarked_claims(answer: str) -> list[str]:
    """The claims in this answer that carry no citation marker.

    Returned rather than counted so a refusal can say *what* was wrong, and so an
    operator inspecting a request can see the sentences — the text is the model's
    own words about the corpus, already in the answer, so nothing new is exposed.
    """
    return [s for s in substantive_sentences(answer) if not has_marker(s)]
