"""Faithfulness grading (T142): calibrated against real answers, not chosen.

The rule is one third content-word overlap **and** two shared words of five or
more characters. Both halves were arrived at by measuring answers this system
actually produced:

- A correctly cited paraphrase of a 32-word passage shares 0.42 of its content
  words with it. A 0.5 threshold called that unfaithful; 1/3 with the
  distinctive-word condition passes it while still failing a claim whose
  vocabulary is disjoint from every passage it cites — the invented-JVM-heap
  sentence below, which scores 0 on a correct answer and drags the ratio to 0.5.
- An answer with no citations at all is 0.0, not null: "grounded in nothing" is
  a measurement, and `null` is reserved for an answer with no claims to grade.
"""

from __future__ import annotations

import pytest

PASSAGE = (
    "Atlassian apps and sites don't have fixed individual IP addresses. Instead, they use "
    "defined ranges of IP addresses. You should allowlist these IP ranges to maintain access "
    "to Atlassian cloud apps."
)
SECOND_PASSAGE = (
    "The relevant Atlassian domains must also be allowlisted so the services used by cloud "
    "apps continue to resolve."
)

ANSWER = (
    "You must allowlist the published IP address ranges used by Atlassian cloud apps and the "
    "relevant Atlassian domains [1] [2]."
)

INVENTED = "Set the JVM heap to 4096 megabytes in the standalone configuration file."


class TestFaithfulness:
    def test_a_cited_paraphrase_is_faithful(self) -> None:
        from app.evaluation.metrics import faithfulness

        assert faithfulness(ANSWER, [PASSAGE, SECOND_PASSAGE]) == 1.0

    def test_an_invented_claim_is_not_faithful(self) -> None:
        from app.evaluation.metrics import faithfulness

        assert faithfulness(f"{ANSWER} {INVENTED}", [PASSAGE, SECOND_PASSAGE]) == 0.5

    def test_an_answer_with_no_citations_scores_zero_not_null(self) -> None:
        from app.evaluation.metrics import faithfulness

        assert faithfulness(ANSWER, []) == 0.0

    def test_an_answer_with_no_claims_is_null(self) -> None:
        from app.evaluation.metrics import faithfulness

        assert faithfulness("", [PASSAGE]) is None
        assert faithfulness("Note.", [PASSAGE]) is None

    @pytest.mark.parametrize(
        "quotes",
        [
            [PASSAGE],
            [PASSAGE, SECOND_PASSAGE],
            ["You should allowlist these IP ranges to maintain access to Atlassian cloud apps."],
        ],
    )
    def test_the_verdict_survives_a_single_passage(self, quotes: list[str]) -> None:
        """The rule must not depend on how many passages were cited."""
        from app.evaluation.metrics import faithfulness

        assert faithfulness(ANSWER, quotes) == 1.0
