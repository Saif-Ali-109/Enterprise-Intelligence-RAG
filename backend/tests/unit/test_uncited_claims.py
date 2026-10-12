"""An uncited claim must not reach a reader (FR-002, Principle I).

The prompt has always told the model to mark every claim and has promised that an
answer whose sentences carry no markers is refused. Nothing enforced the second
half until 2026-10-12. A live answer carried five substantive sentences, three of
them cited, and this one uncited:

    "To set the restrictions, open the Share dialog on the page, choose 'Restrict'
     and select whether you want to limit 'Can view' or 'Can edit' to specific
     users or groups."

The most useful sentence in the answer, with nothing to stop a reader acting on
it. The evaluation suite caught it as a fabricated fact; this file is the runtime
half of the same rule.

**The retry is the point.** Refusing a first answer that merely omitted a marker
would refuse roughly half of answerable questions (measured 2026-10-04: the model
marks claims in about half its answers) over a formatting habit. So the pipeline
asks again inside the attempt loop it already runs for the verifier, and refuses
only when the retry still leaves a claim unmarked.
"""

from __future__ import annotations

import pytest
from app.generation.claims import has_marker, substantive_sentences, unmarked_claims

pytestmark = pytest.mark.unit

CITED = (
    "Open the filter manager and choose Save as filter. [1] A saved filter can be "
    "run again by name from the same screen. [1]"
)

#: The measured answer, with the uncited sentence it should never have had.
UNCITED_PROCEDURE = (
    "In Confluence Cloud you add page restrictions to control who can see or edit a page. "
    "When you apply a view restriction, the page will not appear in the content tree or "
    "macros for anyone without view permission. [1] "
    "To set the restrictions, open the Share dialog on the page, choose 'Restrict' and "
    "select whether you want to limit 'Can view' or 'Can edit' to specific users or groups. "
    "Users who need access can request view or edit rights. [2]"
)


def test_a_fully_marked_answer_has_no_unmarked_claims() -> None:
    assert unmarked_claims(CITED) == []


def test_the_measured_answer_reports_the_procedure_it_should_not_have() -> None:
    """The exact sentence the evaluation flagged, recovered from the real answer."""
    found = unmarked_claims(UNCITED_PROCEDURE)

    assert len(found) == 2, found
    assert any("Share dialog" in s for s in found)


def test_a_framing_restatement_is_also_a_claim_when_it_is_long_enough() -> None:
    """Not every unmarked sentence is a fabrication, and the rule treats them alike.

    "In Confluence Cloud you add page restrictions to control who can see or edit a
    page" asserts nothing the citations would contradict, but the system cannot tell
    that from a claim it can, and the prompt already instructs the model to mark it.
    The honest position is that the rule is blunt: it refuses on unmarked claims and
    lets the retry sort out the ones that were not really claims.
    """
    assert any("add page restrictions" in s for s in unmarked_claims(UNCITED_PROCEDURE))


def test_both_marker_forms_count() -> None:
    """`[E1]` from the prompt and `[1]` from resolution are the same thing."""
    for text in ("Open the filter manager. [E1]", "Open the filter manager. [1]"):
        assert has_marker(text), text
        assert unmarked_claims(text) == []


def test_a_leading_marker_belongs_to_the_sentence_before_it() -> None:
    """`. [1] Claim two.` marks the sentence the marker *follows*.

    Handled because it is the shape live answers take; without it, a correctly
    cited answer reads as one uncited claim followed by one uncited claim, and the
    absolute-zero gate fails on an answer that cites everything.
    """
    answer = "First claim is made here. [1] Second claim is made here. [2]"
    assert unmarked_claims(answer) == []


def test_a_bare_marker_is_not_a_claim() -> None:
    """A marker, a heading or a one-word lead-in must never be graded."""
    assert substantive_sentences("[1] Note:") == []
    assert unmarked_claims("[1] Note:") == []


def test_an_empty_answer_has_no_claims() -> None:
    assert unmarked_claims("") == []


def test_the_predicate_is_shared_with_the_metric() -> None:
    """One definition, so the runtime and the gate cannot disagree.

    The metric imported `fabricated_facts` and the service imported this; a fix
    that reached only one of them would show up as a dashboard disagreeing with the
    product, which is worse than either being wrong alone.
    """
    from app.evaluation.metrics import fabricated_facts

    assert fabricated_facts(UNCITED_PROCEDURE) == len(unmarked_claims(UNCITED_PROCEDURE))
    assert fabricated_facts(CITED) == len(unmarked_claims(CITED)) == 0
