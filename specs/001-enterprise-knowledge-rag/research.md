# Phase 0 Research: Enterprise Knowledge Intelligence RAG

**Date**: 2026-09-27 | **Spec**: [spec.md](./spec.md) | **Plan**: [plan.md](./plan.md)

Every entry below resolves a `NEEDS CLARIFICATION` or records a technology decision with
its rationale and rejected alternatives, per Constitution Principle VII.

---

## R-001: Hosted embedding model — **contradicts the original brief**

**Decision**: default to `llama-text-embed-v2`, **not** `multilingual-e5-large`.

**Rationale**: `multilingual-e5-large` has a **512-token maximum sequence length**. The
brief specifies structure-aware chunks of 600–1000 tokens to keep code examples and
tables intact. Those chunks **exceed the model's input limit** and would be silently
truncated server-side (`truncate: "END"` is the default), so the tail of nearly every
chunk would never reach the embedding — and would never be retrievable evidence for the
answer. The model would then be cited as supporting a claim drawn from text it never saw.
That is a direct Principle I violation (grounded answers, valid citations), not a
ranking-quality preference.

`llama-text-embed-v2` has a 2048-token limit, a 1024 default dimension, and supports
cosine. It accommodates 600–1000 token chunks with headroom for the heading path
prepended to each chunk.

**Alternatives considered**:
- `multilingual-e5-large` with chunks reduced to ~400 tokens — rejected. Fragmenting to fit
  the embedder inverts the pipeline: retrieval quality is supposed to be bounded *upward* by
  chunking quality (Principle III), and ~400 tokens splits most documentation sections
  mid-explanation, working against FR-015/FR-016.
- `multilingual-e5-large` accepting truncation — rejected. Silent data loss in evidence.
- Sparse `pinecone-sparse-english-v0` — rejected. English-only and loses semantic
  generality; corpus is English, but the dense model already covers this well.

**Resolved 2026-09-28 — accepted.** The owner was asked to choose between this and shrinking
chunks to fit `multilingual-e5-large`, and selected `llama-text-embed-v2` on the technical
grounds above. The brief's stated preference is therefore overridden, and the override is
recorded in the spec's `## Clarifications` for `Session 2026-09-28` rather than left as a
planning note. The model remains a configuration value (`PINECONE_EMBED_MODEL`); only the
default is fixed.

**Corroborating detail**: Pinecone's own docs warn that `input_type` mismatch "quietly
degrades" search quality while both calls still succeed. Since `input_type` is fixed at
index-creation time, this is a configuration decision that must be tested, not assumed.

---

## R-002: Integrated-embedding index creation — current API shape

**Decision**: create the index with `create_for_model`, passing cloud, region, and an
`EmbedConfig` that sets `field_map`, `read_parameters.input_type = "query"`, and
`write_parameters.input_type = "passage"`.

```python
from pinecone import Pinecone, EmbedConfig

pc = Pinecone()  # PINECONE_API_KEY from environment
idx = pc.indexes.create_for_model(
    name="enterprise-knowledge",
    cloud="aws",
    region="us-east-1",
    embed=EmbedConfig(
        model="llama-text-embed-v2",     # per R-001
        field_map={"text": "chunk_text"},
        # dimension deliberately omitted -> model default (1024).
        # Principle VII: dimensionality is never hard-coded.
        read_parameters={"input_type": "query", "truncate": "END"},
        write_parameters={"input_type": "passage", "truncate": "END"},
    ),
)
```

**Rationale**: omitting `dimension` satisfies "do not hard-code vector dimensions when
integrated embedding supplies them" (Principle VII). Note there is **no** `dimension="auto"`
sentinel in the current API — the correct way to get the model default is to omit the
argument entirely.

**Documented inconsistency — must be verified before implementation**: current Pinecone
documentation is mid-migration and shows two forms for the same operation. The API reference
documents `create_index_for_model` / `indexes.create_for_model`, while the SDK how-to page
`integrated-records.html` (published 2026-09-09) still shows `pc.indexes.create(spec=IntegratedSpec(embed=EmbedConfig(...)))`.
A v10 SDK deprecation of `IntegratedSpec` was reported during research but **could not be
independently confirmed** from official sources.

**Action**: Phase 3 opens with a smoke test that creates an index both ways and records
which the installed SDK accepts, before any pipeline code depends on it. Do not hard-code
either form until verified against the installed SDK version. Principle VII requires
adapting to the current API rather than preserving an obsolete pattern — and equally, not
inventing a pattern that does not exist.

---

## R-003: Write and read paths

**Decision**: write with `upsert_records` (text + metadata, no `values`); read with
`index.search` using `inputs={"text": ...}`.

```python
idx.upsert_records(
    namespace="atlassian-public",
    records=[{"id": "doc_8a93f#chunk_004", "chunk_text": "...", "product": "jira", ...}],
)

resp = idx.search(
    namespace="atlassian-public",
    top_k=12,                                  # candidate pool (FR-020)
    inputs={"text": rewritten_query},
    filter={"product": {"$in": ["jira", "confluence"]}},
    fields=["chunk_text", "product", "category", "heading_path", "source_url"],
)
```

**Rationale**: `query()` requires a caller-supplied vector and will never embed, so it is
wrong for an integrated-embedding index. `search()` is the read counterpart of
`upsert_records()`. Metadata is flat and filterable; the `field_map` text field is the only
field sent for embedding.

**Gotchas recorded**:
- `upsert_records` has **no default namespace** — it must be passed explicitly.
- `namespace` is not a reserved filter key, so it is excluded from metadata payloads.
- `filter` requires at least one key; an empty `filter={}` is a client-side error.
- Only `$and`/`$or` are permitted at the top level of a filter expression.
- `search` accepts `rerank={...}` for a fused single-round-trip rerank (see R-004).

---

## R-004: Reranking — two implementations behind one interface

**Decision**: define a `Reranker` interface with a Pinecone-hosted implementation using
`bge-reranker-v2-m3`, exposed as a standalone `inference.rerank` call.

**Rationale**: Principle VII requires the stage be swappable; a single fused `search(rerank=…)`
call would couple retrieval and reranking into one vendor round trip and make the
substitution impossible without rewriting the retriever. A separate call keeps the
`Reranker` interface honest and lets the retriever's 12-candidate pool and the reranker's
top-5 be configured and tested independently (FR-019, FR-020).

```python
rr = pc.inference.rerank(
    model="bge-reranker-v2-m3",
    query=original_query,
    documents=[c.text for c in candidates],
    top_n=5,
    return_documents=False,
)
# rr.data[i].score normalised to [0,1]; higher is more relevant
```

**Alternatives considered**:
- Fused `search(rerank=…)` — rejected as above; couples two pipeline stages to one API call.
- `pinecone-rerank-v0` — rejected; reported deprecated and rejected at request time.
- `cohere-rerank-3.5` — viable alternative, available in the same account. Selected model
  stays configurable; the interface does not change if it is swapped.

---

## R-005: Record identity and deletion

**Decision**: vector record IDs are `{document_id}#{chunk_index_zero_padded}`. Deletion uses
`delete(filter=...)` scoped by `document_id`, plus Postgres cascade for the registry.

**Rationale**: gives mechanical traceability from a retrieval hit back to a registry row
(FR-018, FR-024). Filter-scoped deletion is required by FR-031/FR-056 — deleting a document
must remove its vectors, and tombstoning must survive an in-flight crawl.

**Gotcha**: `delete()` takes **exactly one** selector — `ids`, `filter`, or `delete_all`.
Combining them is a client-side error. An empty filter object also errors, so a
document-scoped delete must always carry at least `document_id`.

---

## R-006: Language model provider — Groq, and a stale-docs trap

**Decision**: `groq` Python SDK (1.7.0) behind the `LLMProvider` interface, base URL
`https://api.groq.com/openai/v1`. Default generation model `openai/gpt-oss-120b`;
`openai/gpt-oss-20b` for the cheap classification path (1,000 t/s at a quarter the price).
Model ID lives in config, never in source (Principle VII).

**The trap**: Groq's own quickstart still shows `llama-3.3-70b-versatile`. That model went
**Enterprise-only on 2026-08-16** and returns 401/403 on Free and Developer plans. It is
also the model most RAG tutorials and LLM-generated code name by default. Eleven Groq models
have been shut down in 2026 alone, including the entire `llama-4` and `groq/compound` lines.

**Rationale for the default**: `openai/gpt-oss-120b` (131k context, 65k max completion) gives
ample headroom for a 3–6 chunk evidence set plus system prompt and schema, at $0.15/$0.60 per
1M tokens. The 20b model handles query classification at 1,000 t/s.

**Alternatives considered**:
- `llama-3.3-70b-versatile` — rejected, Enterprise-only, 401 on the target plans.
- `qwen/qwen3.8-27b` — viable at $0.80/$4.00, but 3.3x the input cost of 120b for no
  requirement this project has. Config-swap if needed.
- A Responses-API beta path — rejected; different usage field names for no benefit here.

---

## R-007: Streaming vs. structured output — the constraint that shapes the API

**Decision**: stream **pipeline stage events**, not token deltas. Buffer the model's
structured response and emit it whole in the final event.

**Rationale**: Groq **does not support streaming together with Structured Outputs**. You
cannot feed a `ReadableStream` of tokens into a `json_schema` response format. The naive
design — stream tokens, structure the output — is simply unavailable.

This constraint happens to fit the spec exactly. The event vocabulary in FR-035 is
stage-based: `query_received`, `query_analyzed`, `retrieval_started`,
`retrieval_completed`, `reranking_completed`, `generation_started`,
`citation_validation`, `answer_completed`. None of these is a token delta. The client gets
live feedback through every retrieval and reranking stage, which is where the perceived wait
actually is, and the answer arrives atomically with its validated citations.

Had the spec called for token-by-token streaming, this would have forced a choice between
strict JSON output and live text. It does not, so no trade-off is needed.

**Schema constraints to encode in the generator**:
- `additionalProperties: false` is **required** on every object.
- **Every** property must appear in `required`. An optional field is expressed as a nullable
  type (`["string", "null"]`), never by omitting it from `required`.
- `max_completion_tokens` is current; `max_tokens` is deprecated.
- `logprobs`, `logit_bias`, `store`, and `metadata` return 400 — do not send them.
- `n` must be 1. `temperature: 0` is silently coerced to `1e-8`, so do not expect true
  greedy decoding to be reproducible.

**Strict-schema models**: `openai/gpt-oss-20b`, `openai/gpt-oss-120b`, `qwen/qwen3.8-27b`.
Others get loose JSON mode only, with Pydantic validation on the way out.

---

## R-008: Retry and rate-limit policy

**Decision**: client `max_retries=1`, with an application-level bounded retry loop using full
jitter, and a hard elapsed-time deadline below the request timeout.

| Condition | Action |
|---|---|
| 429 | Honour `retry-after` if present; else exponential backoff with jitter, capped |
| 498 (flex capacity exhausted) | **Do not retry.** Capacity signal, not a throttle — degrade or fail |
| 5xx | Retry freely; these are not billed |
| 400 from a bad model ID or unsupported param | **Never retry.** A config bug will not fix itself |

**Rationale**: SDK retries compound with the application request timeout, so the client
retry count is kept low and bounded retry is owned by the application. The taxonomy matters:
retrying a 498 wastes the whole latency budget on a request that cannot succeed, and retrying
a 400 turns a config error into a 25-second hang. This is what makes SC-017 (95% of answers
within 8s) achievable rather than aspirational.

Token-usage fields for observability (FR-048): `prompt_tokens`, `completion_tokens`,
`total_tokens`, and `total_time`. `total_time` includes queueing and is the honest latency
number to report.

---

## R-009: Content extraction — trafilatura flattens the heading tree

**Decision**: trafilatura (2.2.0) for boilerplate removal, then a **custom heading-path
reconstruction walk** over its normalized body tree.

**The finding**: trafilatura's XML output emits headings as **flat siblings** of paragraphs,
tagged `<head rend="h1">`, `<head rend="h2">`, and so on. The heading *level* survives in
`rend`; the heading *hierarchy does not exist in the output*. Boilerplate removal is
excellent — nav, header, ad asides, related-article blocks, and footers were all correctly
dropped in testing — but anyone reading the docs would reasonably assume `<head>` elements
nest, and they do not.

This is load-bearing for the whole project. FR-018 requires every chunk to carry its
`heading_path`, and FR-015 requires chunk boundaries derived from real document structure. A
flat heading list with no tree cannot satisfy either. The hierarchy must be rebuilt with a
level-indexed stack:

```python
# stack[i] holds the heading text at level i+1
del stack[level - 1:]   # close deeper levels before pushing
stack.append(text)
```

That single line is what makes skipped heading levels work: a document with `# A` then
`### C` (no `## B`) yields `("A", "C")` rather than a wrong three-deep path. This was
verified against a fixture, not inferred.

**Output format note**: valid `output_format` values are
`csv, html, json, markdown, python, txt, xml, xmltei`. `"md"` is **not** valid and raises
`AttributeError`. Use **`xml`** — markdown output derives `#` markers from `rend` and
defaults to level 2 when `rend` is missing, so heading levels become unreliable.

**Block types available in the normalized tree**: `p`, `list` (`rend="ul"|"ol"`), `item`,
`table`, `row`, `cell`, `code`, `quote`, `head`, `hi`, `ref`, `lb`, `graphic`, `div`. A
`list`/`table`/`quote`/`code` is **one block** whose children are its content and must not be
re-emitted as separate blocks.

**Gotchas recorded**:
- `<code>` can nest `<code>` (a `pre > code` artifact), so a naive recursive child walk
  double-counts. Separator-aware text extraction is required rather than bare `itertext()`.
- `load_html()` rejects single-block fragments; wrap partial HTML in `<div>…</div>`.

---

## R-010: HTML parser choice — lxml, and it is free

**Decision**: lxml only. No BeautifulSoup, no selectolax in the extraction path.

**Rationale**: trafilatura already hands back lxml elements, so lxml means **zero re-parsing**.
Measured on a fixture: lxml 0.12 ms, selectolax 0.13 ms, BeautifulSoup 1.77 ms — bs4 is ~14x
slower. Adding selectolax or bs4 would buy nothing and cost a conversion step, which is
exactly the dependency bloat Constitution Principle VII asks to be justified.

selectolax remains defensible **only** for CSS-selector queries on *pre*-extraction raw HTML
(reading `<meta>` or JSON-LD), not for the body walk. If it is ever used there, note that
`css("h1, h2, h3")` returns nodes **grouped by selector, not in document order** — which
would silently corrupt heading paths.

---

## R-011: Frontend transport — EventSource cannot POST

**Decision**: Next.js 16 App Router `route.ts` returns a raw `ReadableStream`; the client
consumes it with `fetch` + `ReadableStream` reader, parsing SSE frames manually.

**Rationale**: `EventSource` issues GET only and cannot send a body or set headers, so it
cannot carry a question. The server half is documented Next.js behaviour; the client half is
a platform-standard pattern, not Next.js-documented (flagged as such).

**Stack pins** (current stable, verified against the npm registry): `next@16.3.6`,
`react@19.3.0`, `tailwindcss@4.3.3` + `@tailwindcss/postcss`, `@tanstack/react-query@5.104.0`,
shadcn CLI `4.21.0`. Node 20.9+, TypeScript 5.1+.

**Gotchas that will cause silent breakage**:
- `decoder.decode(value, { stream: true })` is **required**, or multi-byte characters split
  across chunk boundaries are corrupted.
- `cache: "no-store"` on the client fetch — Next's fetch cache and the browser cache both
  break streaming.
- `Cache-Control: no-cache, no-transform` and `X-Accel-Buffering: no` on the response, or
  intermediate proxies buffer the stream and events arrive in a clump.
- Next enables gzip by default; set `compress: false` in `next.config.js` if a proxy in front
  already compresses.
- `runtime = "nodejs"`; route params are **Promises** in Next 15+/16.
- `export const dynamic` is **removed** under `cacheComponents: true`. POST handlers are
  dynamic by default, so it is omitted unless caching is deliberately disabled.
- Verify with `curl -N` before suspecting application code when events arrive late.

---

## R-012: Accessibility verification

**Decision**: verify FR-057–FR-060 with an automated axe-core pass over each view plus a
manual keyboard-only walk, rather than claiming conformance untested.

**Rationale**: SC-022 promises an automated check reporting no serious or critical
violations. Principle VI forbids reporting quality that was not actually measured — the same
rule that governs evaluation metrics governs accessibility claims. Tailwind v4 has no
ready-made contrast tokens, so contrast is an explicit check, not an inherited default.

---

## R-013: Token counting — no new dependency, but a *measured* margin

**Decision**: size retrievable units with a deterministic local estimator; do not add a
tokenizer dependency. Calibrate it against a reference tokenizer and apply an explicit
safety margin to the hard limit.

**Rationale**: two pressures point the same way. First, the embedder is **hosted** — its
tokenizer is not exposed to us, so *any* local count is an approximation of a token
definition we cannot see. Adopting `tiktoken` would buy precision measured against a
tokenizer that is not the one doing the embedding. Second, the arithmetic has slack: a
600–1000 token target against a 2048-token hard limit is 2x headroom (R-001), which easily
absorbs estimator error. FR-015 forbids splitting on a fixed *character* count, and a
token-estimating heuristic is not that — structure still decides boundaries; the estimate
only decides when a structural section is too large to remain one unit.

**The margin must be measured, not asserted.** Phase 7 carries a test that pins the
estimator's error against a reference tokenizer over a fixture of real documentation
blocks (prose, tables, code) and fails above 10%. If calibration fails, the fallback is to
adopt the reference tokenizer — recorded here so the decision is reversible on evidence
rather than on preference.

**Safety application**: the hard limit is not 2048. Effective ceiling is
`hard_limit × 0.75`, so a unit that the estimator believes is 1600 tokens is split even
though the model would technically accept it.

---

## R-014: Suppressing model reasoning — an FR-034 leak that is easy to walk into

**Decision**: explicitly disable reasoning exposure on **every** provider call, and make
the response parser drop any unknown key rather than pass it through.

**Rationale**: Groq's current default generation models are **reasoning** models. The API
exposes `reasoning_format` (`hidden` / `raw` / `parsed`) and an `include_reasoning` toggle,
and `reasoning_effort` defaults to `medium` for the gpt-oss family. Left alone, raw
reasoning can come back in the message content — and any of it that reaches a response,
an SSE event, or a log line is a direct FR-034 violation, the one requirement the spec
treats as absolute ("MUST NOT expose private model reasoning or internal deliberation in
any response, event, or view").

The default is the hazard here, not an explicit opt-in. Three controls, all required:

1. **Provider call**: set `include_reasoning=False` and `reasoning_format="hidden"`
   explicitly. Do not rely on a default.
2. **Response parser**: unmarshal into the declared Pydantic model and discard unknown
   fields. Never pass a raw provider dict through to a response or event.
3. **Contract test**: the event schemas in [contracts/events.md](./contracts/events.md)
   contain no reasoning, thought, or deliberation field, and a test asserts the generator's
   request payload cannot request reasoning.

**Latency note**: `reasoning_effort` defaults to `medium` on gpt-oss models, which spends
tokens and wall-clock time even when the reasoning is hidden and discarded. Setting
`low` (or `none` where supported) on the query-classification path removes that cost and
directly supports SC-017. Classification is a cheap judgement task; extended deliberation
buys nothing.

---

## Unresolved — carried into implementation, not deferred silently

| Item | Status | Owner phase |
|---|---|---|
| Exact `EmbedConfig` construction form (`create_for_model` vs `IntegratedSpec`) | **Documented inconsistency in vendor docs**; v10 deprecation reported but unconfirmed. Resolve by smoke test | Phase 3 |
| Groq Developer-plan rate limits | Partially behind a client-rendered tab; figures taken from a secondary table. Re-check before capacity planning | Phase 12 |
| `gpt-oss-safeguard-20b` strict-schema support | Vendor labels it "best effort". Must not underpin a correctness-critical extraction path | Phase 12 |
| Rerank model entitlement (`bge-reranker-v2-m3` vs `cohere-rerank-3.5`) | Account-dependent; interface is fixed either way | Phase 10 |
| Embedding models actually enabled on the account | `pc.inference.list_models()` is authoritative, not the docs. `llama-text-embed-v2` chosen as default but not yet confirmed entitled | Phase 3 |

**Resolved 2026-09-28** — embedding model (`llama-text-embed-v2`), language model pair
(`openai/gpt-oss-120b` generation, `openai/gpt-oss-20b` classification), and the nine-table
persistence model were all decided by the owner. They are no longer open items. The gate
targets became configuration with the stated defaults (FR-065), with the absolute zeros
explicitly excluded from that flexibility (FR-066).
