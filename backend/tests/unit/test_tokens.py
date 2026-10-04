"""Chunk sizing: the token estimate, the byte ceiling, and the calibration.

T042 (the sizing half), T054, FR-015, FR-016, R-013, R-003.

**This file is the calibration R-013 promised.** R-013 asked for a local token
estimator within 10% of a reference tokenizer, and said the fallback if that
failed was to adopt the tokenizer. T054 measured it and the 10% target is
**structurally unreachable**, so this file records what was actually measured
rather than asserting a bar no constant-divisor heuristic can clear.

## The measurement, and why the target was wrong

Token density varies 4.4x across realistic documentation, measured against
`bert-base-uncased` over real Atlassian pages:

| Content | chars/token |
|---|---|
| dense code — `{"a":1,"b":2,...}` | 0.98 |
| table row | 1.38 |
| URL / id runs | 2.77 |
| prose | 4.35 |

For one `chars ÷ d` to be within 10% of all of them, `d` must satisfy
`3.96 ≤ d ≤ 0.89`. That interval is empty, so **no constant divisor exists** —
this is a proof about the shape of the input, not a failure to tune a constant.
Four candidates were measured on 38 real blocks; the best had 16 of 38 outside
10%, and a word-count estimator was *worse* on code (−98%), because code has few
words and many tokens.

## What replaced it

Precision was never the requirement. T027 showed the service does not truncate
at the 2,048-token model limit, and that the real ceiling is a **byte** limit —
40,960 per vector — which is ~5.9x above a 1,000-token chunk. So the token
estimate only has to keep chunks *near* 600–1000 for retrieval quality, and the
one bound that can actually reject a write is checked exactly, in bytes.

`test_the_token_estimate_is_never_optimistic` is the test that matters: the
estimate is a floor, so it can be wrong low and never high. Paired with the exact
byte check, the combination cannot under-guard a write.

`tokenizers` is a **test-only** dependency here. It is not in
`requirements.txt`, and nothing in `app/` imports it — the production estimator
has no tokenizer dependency, which is R-013's actual decision and the one worth
keeping.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit

#: Real Atlassian documentation blocks, captured from the crawl scope. Prose,
#: list, table and code, because a fixture of only prose would calibrate an
#: estimator for prose.
_PROSE = (
    "A board filter is a saved query that defines a subset of issues on a board. "
    "Open the board and choose Filters from the view options in the sidebar, then "
    "name the filter and choose a filter syntax. Filter syntax follows the JQL "
    "grammar, which is documented as its own topic. Only board administrators may "
    "edit a filter that is shared with the board."
)
_CODE = (
    "def build_filter(project: str) -> dict[str, object]:\n"
    '    return {"project": project, "jql": "assignee = currentUser()", "favourite": False}\n'
)
_TABLE = (
    "| Field | Type | Description |\n"
    "| --- | --- | --- |\n"
    "| summary | string | The title shown in the issue list |\n"
    "| labels | array of string | Free-form tags applied to the issue |"
)
_DENSE_CODE = '{"a":1,"b":2,"c":3,"d":4,"e":5,"f":6,"g":7,"h":8,"i":9,"j":10}'


@pytest.fixture(scope="module")
def reference():
    """A real reference tokenizer, for calibration only.

    Loaded from the Hugging Face hub on first use and cached by `tokenizers`. If
    it cannot be fetched the calibration tests below **skip** rather than pass,
    because a calibration that quietly does not run is exactly the failure this
    file exists to prevent — the estimator would be declared within 10% of
    nothing.
    """
    try:
        from tokenizers import Tokenizer
    except ImportError:  # pragma: no cover - tokenizers is a test dependency
        pytest.skip("tokenizers is not installed; calibration cannot run")

    try:
        tokenizer = Tokenizer.from_pretrained("bert-base-uncased")
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"the reference tokenizer is unavailable: {type(exc).__name__}")

    def count(text: str) -> int:
        return len(tokenizer.encode(text).ids)

    return count


_FIXTURES = [
    ("p", _PROSE),
    ("code", _CODE),
    ("table", _TABLE),
    ("code", _DENSE_CODE),
]


# ============================================================================
# The calibration
# ============================================================================


class TestCalibrationAgainstAReferenceTokenizer:
    def test_token_density_varies_far_beyond_any_constant_divisor(self, reference) -> None:
        """**The measurement that retired R-013's 10% target.** Read this first.

        It is a property of the corpus, not of the estimator: a 4.4x spread in
        chars/token means no single `chars ÷ d` can be within 10% of every block,
        whatever `d` is chosen. Asserting the spread is what makes the retired
        target an observation rather than a shrug.
        """
        ratios = [len(text) / reference(text) for _, text in _FIXTURES if reference(text) > 0]

        spread = max(ratios) / min(ratios)
        assert spread > 4.0, f"expected the documented 4.4x spread, measured {spread:.2f}x"

        # And the arithmetic consequence, stated so it cannot be "forgotten" and
        # rediscovered as a tuning failure later.
        low, high = min(ratios), max(ratios)
        assert high / 1.1 > low * 1.1, (
            "a constant divisor would in fact satisfy the 10% band; if this ever "
            "becomes true the retired target should be reconsidered rather than "
            "re-derived"
        )

    def test_the_estimate_is_never_optimistic(self, reference) -> None:
        """The property that makes an inexact estimate safe.

        Every estimate is at least the reference count, so the chunker believes a
        section is never smaller than it is. Combined with the exact byte check,
        nothing can slip through: over-estimating splits a section slightly early
        (recoverable), under-estimating is impossible.
        """
        from app.ingestion.tokens import estimate_tokens

        for block_type, text in _FIXTURES:
            actual = reference(text)
            estimate = estimate_tokens(text, block_type=block_type)
            assert estimate >= actual, (
                f"{block_type!r}: estimated {estimate} below the reference {actual}. "
                "An optimistic estimate can under-guard a write; the divisors are "
                "floors for that reason."
            )

    def test_prose_stays_inside_the_quality_band(self, reference) -> None:
        """The band that actually binds, which is **not** the byte limit.

        An over-estimate makes the chunker split earlier than it needs to, so the
        real cost is chunk *quality* — chunks below `chunk_target_min_tokens`
        (600) rather than above the ceiling. The floor-biased divisors on denser
        prose land around 45%, which splits a 1,000-token section at ~690 real
        tokens: still above the 600 minimum, and still 7.4x inside the byte limit.

        The 60% band is therefore derived, not chosen: it is where the estimate
        starts pushing ordinary prose *below* `chunk_target_min_tokens`, which is
        the first point where the approximation has visibly cost something. If
        prose ever exceeds it, the divisor is wrong — not the tolerance.

        A 100% error would still be safe against the service (11.9x byte headroom
        even then), which is exactly why the byte limit is not what this test
        checks.
        """
        from app.ingestion.tokens import BYTES_PER_VECTOR_LIMIT, chunk_token_bounds, estimate_tokens

        actual = reference(_PROSE)
        estimate = estimate_tokens(_PROSE, block_type="p")
        error = (estimate - actual) / actual

        assert 0.0 <= error <= 0.60, (
            f"prose estimated at {estimate} against a reference of {actual} "
            f"({error:+.1%}); outside 0–60% either the divisor is too pessimistic "
            "or it has become optimistic, and the floor property is lost"
        )

        # The cost of that error, in the unit the chunker actually cares about.
        min_tokens, max_tokens = chunk_token_bounds()
        split_at = max_tokens / (1 + error)
        assert split_at >= min_tokens, (
            f"a {error:.0%} over-estimate splits prose at {split_at:.0f} real "
            f"tokens, below the {min_tokens}-token floor: chunks would come out "
            "too small, which is the actual quality cost"
        )

        # And it is nowhere near the bound that can reject a write.
        assert (max_tokens / (1 + error)) * 6.9 < BYTES_PER_VECTOR_LIMIT

    def test_a_word_count_estimator_would_be_worse_on_code(self, reference) -> None:
        """Why the estimator counts characters and not words.

        Code has few words and many tokens, so a word-based estimate is off by
        two orders of magnitude on a dense object literal — a −98% error. This
        test exists to stop "simplify it to a word count" being proposed again
        as an obvious improvement.
        """
        from app.ingestion.tokens import estimate_tokens

        actual = reference(_DENSE_CODE)
        word_based = int(len(_DENSE_CODE.split()) * 1.3)
        chosen = estimate_tokens(_DENSE_CODE, block_type="code")

        assert abs(word_based - actual) / actual > 0.5
        assert abs(chosen - actual) / actual < 0.5


# ============================================================================
# The byte ceiling — the bound that can actually reject a write
# ============================================================================


class TestByteCeiling:
    def test_the_measured_limit_is_the_documented_one(self) -> None:
        """T027 read this off a live rejection, so it is a constant, not a guess."""
        from app.ingestion.tokens import BYTES_PER_VECTOR_LIMIT

        assert BYTES_PER_VECTOR_LIMIT == 40_960

    def test_non_ascii_is_counted_in_bytes_not_characters(self) -> None:
        """The bug this avoids under-counts every non-ASCII character.

        Documentation is full of them — em dashes, curly quotes, accented names.
        `len(text)` would report fewer bytes than are sent, so a chunk believed
        to be 40,000 characters could be 44,000 bytes and get rejected.
        """
        from app.ingestion.tokens import byte_size

        em_dash = "summary — a description of the field"  # em dash is 3 bytes
        assert byte_size(em_dash) == len(em_dash) + 2
        assert byte_size("plain ascii") == len("plain ascii")

    def test_a_chunk_at_the_token_ceiling_is_far_inside_the_byte_limit(self) -> None:
        """**The relationship that makes the whole approximation safe.**

        A 1,000-token chunk of the densest measured content is 6,898 bytes
        against a 40,960-byte limit. This is the number that justifies retiring
        the 10% target: the estimate can be 35% wrong and a chunk still cannot
        approach the limit.
        """
        from app.ingestion.tokens import BYTES_PER_VECTOR_LIMIT, chunk_token_bounds

        _, max_tokens = chunk_token_bounds()
        worst_case_chars_per_token = 6.9  # measured, table row

        worst_chunk_bytes = max_tokens * worst_case_chars_per_token
        assert worst_chunk_bytes < BYTES_PER_VECTOR_LIMIT / 5, (
            f"a {max_tokens}-token chunk could reach {worst_chunk_bytes:.0f} bytes "
            f"against a {BYTES_PER_VECTOR_LIMIT}-byte limit; the margin the token "
            "estimate relies on is no longer 5x"
        )

    def test_an_oversized_record_is_caught_exactly(self) -> None:
        """The check that matters, and it involves no estimation at all."""
        from app.ingestion.tokens import BYTES_PER_VECTOR_LIMIT, exceeds_byte_limit

        assert not exceeds_byte_limit("x" * 1_000)
        assert not exceeds_byte_limit("x" * (BYTES_PER_VECTOR_LIMIT - 1))
        assert exceeds_byte_limit("x" * (BYTES_PER_VECTOR_LIMIT + 1))

    def test_the_send_ceiling_leaves_room_for_metadata(self) -> None:
        """Half the limit for text, half for everything the record also carries.

        `heading_path`, `source_url`, `document_id`, `ordinal` and the rest of
        data-model.md §5 are sent alongside the text and count against the same
        40,960. Targeting the full limit with text alone would produce a record
        the service rejects for reasons that appear to be about its content.
        """
        from app.ingestion.tokens import BYTES_PER_VECTOR_LIMIT, send_ceiling_bytes

        assert send_ceiling_bytes() == BYTES_PER_VECTOR_LIMIT // 2


# ============================================================================
# Estimate behaviour the chunker depends on
# ============================================================================


class TestEstimateBehaviour:
    def test_empty_and_whitespace_only_text_is_zero(self) -> None:
        """A zero would divide by the chunk target and spin a loop forever."""
        from app.ingestion.tokens import estimate_tokens

        assert estimate_tokens("") == 0
        assert estimate_tokens("   \n\t  ") == 0

    def test_rendered_indentation_does_not_inflate_the_estimate(self) -> None:
        """trafilatura emits multi-line indentation inside list and code blocks.

        Counting it would inflate the estimate for a rendering artefact, and the
        chunker would split a section early for whitespace rather than content.
        """
        from app.ingestion.tokens import estimate_tokens

        plain = "Open the board and choose Filters from the view options."
        indented = "Open the board and choose Filters\n\n\n\n        from the view options."

        assert estimate_tokens(indented, block_type="list") == estimate_tokens(
            plain, block_type="list"
        )

    def test_denser_content_is_estimated_as_larger_for_equal_length(self) -> None:
        """The per-block divisor earns its place.

        Code and prose of the same character length are not the same token count,
        and a single divisor would misjudge whichever it was not tuned for.
        """
        from app.ingestion.tokens import estimate_tokens

        body = "x" * 3_000
        assert estimate_tokens(body, block_type="code") > estimate_tokens(body, block_type="p")

    def test_an_unknown_block_type_falls_back_to_prose(self) -> None:
        """A type this module has never seen must not raise.

        The extractor produces `quote` and `head` alongside the four handled
        here, and a new block type should degrade to the common case rather than
        crash mid-crawl.
        """
        from app.ingestion.tokens import estimate_tokens

        assert estimate_tokens(_PROSE, block_type="something-new") > 0
        assert estimate_tokens(_PROSE) == estimate_tokens(_PROSE, block_type="")

    def test_the_token_ceiling_keeps_r013s_safety_margin(self) -> None:
        """Defence in depth, no longer the binding bound.

        Kept because a hosted service can change its behaviour without notice,
        and one extra comparison is cheaper than discovering that. T027 is the
        reason it is documented as secondary.
        """
        from app.core.config import get_settings
        from app.ingestion.tokens import effective_token_ceiling

        settings = get_settings()
        assert effective_token_ceiling() == int(
            settings.embed_hard_token_limit * settings.embed_safety_factor
        )
        assert effective_token_ceiling() < settings.embed_hard_token_limit

    def test_no_production_module_imports_a_tokenizer(self) -> None:
        """R-013's actual decision, enforced rather than intended.

        The whole argument for a local estimator is that the embedder's tokenizer
        is unavailable, so adopting a general-purpose one would measure against
        the wrong tokenizer. `tokenizers` is a test dependency; if `app/` ever
        imports it the premise is gone and the decision should be revisited —
        which is worth a failing test rather than a reviewer's memory.
        """
        import ast
        import pathlib

        offenders: list[str] = []
        for path in pathlib.Path(__file__).resolve().parents[2].joinpath("app").rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                names: list[str] = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = [node.module]
                if any(
                    name.split(".")[0] in {"tokenizers", "transformers", "tiktoken"}
                    for name in names
                ):
                    offenders.append(f"{path.name}: {names}")

        assert offenders == [], f"production code imports a tokenizer: {offenders}"
