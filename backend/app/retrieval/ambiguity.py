"""Cross-product ambiguity, stated rather than resolved (T098 / edge case 13).

A question that names two products — "can I share a template between Jira and
Confluence?" — has two honest answers and a third dishonest one. The dishonest
one is picking whichever product the classifier ranked first and searching only
that: the user gets a confident answer to half their question and never learns
that the other half went unsearched.

So this module does one thing: **decide whether a question spans products, and say
which ones.** It does not search, and it does not choose.

Two decisions worth reading the reasoning for:

**`developer` is dropped when a specific product is also named.** Atlassian's
developer documentation is hosted under `/cloud/jira/platform/…`, so a page that
says "Jira REST API" also contains the word "developer" somewhere in its URL. A
detector that counted both would announce ambiguity on nearly every question in
this corpus, and a note that is almost always wrong stops being read — the same
failure mode as the stale `_PENDING` list.

**Ambiguity suppresses the product filter rather than widening it.** This is
FR-009/FR-010 applied to the case where the analysis was *right* and still
narrowing was wrong: the analysis says `jira`, and the question says Jira and
Confluence. The filter would silently drop the Confluence half. So the detected
product is recorded, the filter is not applied, and `ambiguity_note` carries what
was found — the ambiguity is reported rather than resolved (FR-009: explicit
unknown, never a forced value).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.core.config import ProductDomain

#: The specific domains. `developer` is the super-domain and is handled
#: separately, because Atlassian hosts developer documentation *inside* each
#: product's section and counting both would make every question ambiguous.
_SPECIFIC = (ProductDomain.JIRA, ProductDomain.CONFLUENCE, ProductDomain.JSM)

#: What a person actually writes, mapped to the domain it means. Longest first, so
#: "jira service desk" is matched before "jira" and a question about JSM is not
#: reported as a Jira question.
_ALIASES: dict[str, ProductDomain] = {
    "jira service desk": ProductDomain.JSM,
    "jira service management": ProductDomain.JSM,
    "service desk": ProductDomain.JSM,
    "jira software": ProductDomain.JIRA,
    "jira": ProductDomain.JIRA,
    "confluence": ProductDomain.CONFLUENCE,
    "jsm": ProductDomain.JSM,
    "developer": ProductDomain.DEVELOPER,
    "rest api": ProductDomain.DEVELOPER,
    "api docs": ProductDomain.DEVELOPER,
}


@dataclass(frozen=True, slots=True)
class AmbiguityDecision:
    """Whether the question spans products, and which ones it names.

    `note` is `None` when there is no ambiguity, and a sentence naming the
    products when there is. It is written for a reader, because it goes into
    `query_analysis.ambiguity_note` and onto the screen.
    """

    ambiguous: bool
    products: list[str]
    note: str | None

    def as_note(self) -> str | None:
        return self.note


def products_named(question: str) -> list[str]:
    """Every product the question names, in order of appearance.

    Word-bounded so "api" does not match "capital" and "jsm" does not match a
    substring of a longer identifier.
    """
    text = question.lower()
    found: list[tuple[int, str]] = []
    claimed: list[tuple[int, int]] = []

    for alias in sorted(_ALIASES, key=len, reverse=True):
        for match in re.finditer(rf"(?<![\w-]){re.escape(alias)}(?![\w-])", text):
            span = match.span()
            # A longer alias already claimed this span: "jira service desk" wins
            # over "jira", and the generic term must not add a second product.
            if any(start <= span[0] and span[1] <= end for start, end in claimed):
                continue
            claimed.append(span)
            found.append((span[0], _ALIASES[alias].value))

    found.sort()
    return list(dict.fromkeys(product for _position, product in found))


def resolve_ambiguity(question: str, detected_product: str | None) -> AmbiguityDecision:
    """Decide whether the question spans products.

    `detected_product` is the analyser's answer, and it does not break a tie:
    a question naming two products is ambiguous even when the classifier is
    confident about one of them, because the classifier was asked to pick one and
    being good at picking one is not evidence the question only meant one.
    """
    named = products_named(question)
    specific = [p for p in named if p in {d.value for d in _SPECIFIC}]

    if specific:
        # The super-domain is where the other product's developer docs live, so
        # it adds nothing to a set that already names a product.
        effective = specific
    else:
        effective = named

    if len(effective) < 2:
        return AmbiguityDecision(ambiguous=False, products=named, note=None)

    listed = " and ".join(effective)
    note = (
        f"This question mentions {listed}, so it was searched across both. "
        "The answer may come from only one of them."
    )
    return AmbiguityDecision(ambiguous=True, products=effective, note=note)


__all__ = ["AmbiguityDecision", "products_named", "resolve_ambiguity"]
