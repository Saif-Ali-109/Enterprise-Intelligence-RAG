"""Local size estimation — for deciding when a section is too big to stay one unit.

T054. FR-015, FR-016, FR-017, R-013, R-001, R-003.

No tokenizer, and that is now a measured decision rather than a preference. This
module answers two different questions and they have very different tolerances:

| Question | Bound | How it is answered here |
|---|---|---|
| Will the service accept this record? | **40,960 bytes/vector** | `exceeds_byte_limit` — exact, no estimation |
| Is this section too big to be one unit? | 600–1000 tokens | `estimate_tokens` — an estimate, error tolerated |

**Only the first one is a safety property.** T027 measured that the hosted embed
service does *not* truncate at the model's 2,048-token limit — a marker in the
final bytes of a 38,350-byte record is still findable — and that the real
ceiling is a byte limit which **rejects** the write above 40,960 bytes. So the
danger is never a silently incomplete embedding; it is a hard ingest error. And a
byte limit needs no tokenizer to check: `len(text.encode("utf-8"))` is exact.

**Why the token estimate is allowed to be approximate.** R-013 originally asked
for a local estimator within 10% of a reference tokenizer. Measured against
`bert-base-uncased`, that target is unreachable by any constant-divisor
heuristic: token density varies **4.4x** across realistic documentation, from
0.98 chars/token in dense code to 4.35 in prose, so a divisor would need to
satisfy `3.96 ≤ d ≤ 0.89` — an empty interval. Four candidates were measured over
38 real Atlassian blocks and the best still had 16 of 38 outside 10%.

The 10% figure is therefore retired rather than met, and the precision it wanted
is unnecessary: a 1,000-token chunk is at most **6,898 bytes** in the worst
measured case, against a 40,960-byte limit — 5.9x of headroom. The estimate
therefore only has to be good enough to keep chunks *near* the 600–1000 target
for retrieval quality. It does not have to be able to tell 999 from 1,001.

**What the estimate is still used for.** Deciding when a structural section is
too large to remain one unit, so the chunker can split it (FR-015). Structure
decides where a boundary goes; the estimate only decides whether a boundary is
needed at all. That is why the estimator is deliberately coarse — an over-estimate
splits a section slightly early, and an under-estimate lets one run slightly long,
and both are recoverable, whereas a split placed by character count would be
arbitrary (FR-015 forbids that outright).
"""

from __future__ import annotations

import re
from typing import Final

from app.core.config import get_settings

#: Fallback for the measured service limit, used only when settings cannot be
#: read. It is a service constant — T027 read it off a live rejection:
#: `Invalid record: Metadata size is 76009 bytes, which exceeds the limit of
#: 40960 bytes per vector` — so it is duplicated here only as a last resort, and
#: the settings value is authoritative wherever settings are available.
BYTES_PER_VECTOR_LIMIT: Final[int] = 40_960

#: Fallback fraction of that limit one record's text may occupy, matching
#: `embed_text_byte_fraction`. See `app/core/config.py` for why it is not 1.0:
#: the record's metadata travels in the same 40,960 bytes.
SEND_TARGET_FRACTION: Final[float] = 0.5

#: chars/token per block type, measured against `bert-base-uncased` over 41 real
#: Atlassian documentation blocks (T054): 28 prose, 6 list, 5 table, 3 code.
#!
#: The direction of the error is the whole point. These are deliberately set
#: **below the minimum observed for the type**, so no block of that type is ever
#: believed to be smaller than the densest one measured:
#!
#: | type | min | p25 | mean | max | divisor here |
#: |---|---|---|---|---|---|
#: | `code`   | 0.98 | 0.98 | 1.90 | 2.64 | 0.93 |
#! | `list`   | 3.10 | 4.24 | 4.33 | 4.88 | 2.95 |
#! | `p`      | 3.33 | 4.12 | 4.30 | 5.14 | 3.17 |
#! | `table`  | 2.68 | 3.41 | 4.98 | 6.90 | 2.54 |
#!
#: Each is `min × 0.95`. Setting them at the minimum exactly would be equally
#: safe and needlessly pessimistic; going *above* the minimum would break the
#: floor property that `test_the_estimate_is_never_optimistic` asserts, and an
#: optimistic estimate is the one failure mode that matters — it can under-guard
#: a write. The `code` divisor is far below its mean because a dense object
#: literal is 0.98 chars/token against 2.64 for ordinary code, and one outlier
#! is enough to make a single constant wrong by 2.7x.
#!
#: The error this costs is real and bounded: at the configured 1,000-token
#: ceiling, over-estimating means a chunk is believed to be up to ~1,900 tokens
#: and is therefore split earlier than strictly necessary. That is the right
#: direction — chunks slightly under target retrieve better than chunks over it,
#! and the byte ceiling is checked exactly regardless.
_CHARS_PER_TOKEN: Final[dict[str, float]] = {
    "code": 0.93,
    "list": 2.95,
    "p": 3.17,
    "quote": 3.17,
    "heading": 3.17,
    # A table's density varies most of any type (2.68 to 6.90), so an unknown
    # type is *not* given the table divisor — that would be 24% pessimistic on
    # prose. Prose is the common case and the better default; a table arriving
    # here is under-estimated, and the byte check catches it exactly.
    "table": 2.54,
    "": 3.17,
}

_DEFAULT_CHARS_PER_TOKEN: Final[float] = 3.17

_WHITESPACE = re.compile(r"\s+")


def byte_size(text: str) -> int:
    """The exact number of bytes the service will measure `text` as.

    UTF-8, because that is what the request is encoded in. Using `len(text)`
    would under-count every non-ASCII character, and documentation is full of
    them — em dashes, curly quotes, and accented names. Under-counting here
    means believing a record is smaller than it is, which is precisely the
    direction that produces a rejected write.
    """
    return len(text.encode("utf-8"))


def exceeds_byte_limit(text: str, *, limit: int | None = None) -> bool:
    """Whether `text` alone is too large for the service to accept.

    The check that matters, and the only one that is exact. Everything else in
    this module is estimation; this is arithmetic.

    Deliberately counts the text alone and not the metadata that will accompany
    it. That is a small under-count, and it is the safe direction combined with
    `embed_text_byte_fraction`: the chunker targets half the limit, so the
    metadata and the JSON overhead land inside the remaining half with room.

    The limit comes from settings when available, so an operator reading a
    changed service limit changes one variable rather than this module.
    """
    return byte_size(text) > (limit if limit is not None else byte_limit())


def byte_limit() -> int:
    """The service's per-vector byte limit, from settings with a fallback.

    Read through `get_settings` so there is one place to change it. The fallback
    is only for a caller that cannot reach settings at all — a settings failure
    should not stop a size check, and a stale-but-close constant is better than
    raising from inside a utility.
    """
    try:
        return get_settings().embed_bytes_per_vector
    except Exception:  # noqa: BLE001
        return BYTES_PER_VECTOR_LIMIT


def send_ceiling_bytes() -> int:
    """The comfortable byte size for one record's text, before its metadata."""
    try:
        settings = get_settings()
        return int(settings.embed_bytes_per_vector * settings.embed_text_byte_fraction)
    except Exception:  # noqa: BLE001
        return int(BYTES_PER_VECTOR_LIMIT * SEND_TARGET_FRACTION)


def estimate_tokens(text: str, *, block_type: str = "") -> int:
    """Approximate the token count of `text`, for sizing decisions only.

    A floor-biased estimate: it reports how many tokens the text is *at least*
    as long as, given the densest content measured for its block type. Never
    claims precision it does not have, and never under-guards, because the byte
    limit is checked separately and exactly.

    `block_type` selects the chars/token divisor. Passing it is worth the effort:
    prose and dense code differ by 4.4x in density, so a single divisor would be
    wrong by that factor somewhere in every chunk containing both.
    """
    divisor = _CHARS_PER_TOKEN.get(block_type, _DEFAULT_CHARS_PER_TOKEN)
    # Whitespace runs are collapsed first because they are cheap in every
    # tokenizer, and trafilatura emits multi-line indentation inside list and
    # code blocks. Counting it would inflate the estimate for a rendering
    # artefact rather than for content.
    normalised = _WHITESPACE.sub(" ", text).strip()
    if not normalised:
        return 0
    return int(len(normalised) / divisor)


def effective_token_ceiling() -> int:
    """The token ceiling, with R-013's safety margin applied.

    Retained as defence in depth. T027 showed the service does not truncate at
    the 2,048-token limit, so this is no longer the binding bound — the byte
    limit is — and it is kept because a hosted service's behaviour can change
    without notice, and a second ceiling costs one comparison.

    The margin is R-013's 0.75: a unit believed to be 1,536 tokens is split even
    though the model would technically accept 2,048.
    """
    settings = get_settings()
    return int(settings.embed_hard_token_limit * settings.embed_safety_factor)


def chunk_token_bounds() -> tuple[int, int]:
    """The configured 600–1000 target, from settings rather than duplicated."""
    settings = get_settings()
    return (settings.chunk_target_min_tokens, settings.chunk_target_max_tokens)


__all__ = [
    "BYTES_PER_VECTOR_LIMIT",
    "SEND_TARGET_FRACTION",
    "byte_limit",
    "byte_size",
    "chunk_token_bounds",
    "effective_token_ceiling",
    "estimate_tokens",
    "exceeds_byte_limit",
    "send_ceiling_bytes",
]
