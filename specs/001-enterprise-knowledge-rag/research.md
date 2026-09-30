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

> ### RESOLVED 2026-09-29 — by introspection of the installed SDK (`pinecone==10.0.0`)
>
> The deferred question above is answered, and the answer is that **one of the two documented
> forms does not exist in the pinned version**. This is a static result: it was obtained by
> introspecting the installed package, with no API key and no network call, so it is a fact
> about the SDK rather than an inference from documentation.
>
> ```
> pinecone.__version__                        -> '10.0.0'
> hasattr(Pinecone, 'create_for_model')        -> False    # the documented name is absent
> hasattr(Pinecone, 'create_index_for_model') -> True
> hasattr(pinecone, 'IntegratedSpec')         -> True
> hasattr(pinecone, 'ServerlessSpec')         -> True
> ```
>
> The two forms are not alternatives. `create_index_for_model` is a **convenience method on
> `Pinecone`** taking the embedding config as a direct argument:
>
> ```python
> Pinecone.create_index_for_model(
>     self, name: str, cloud, region, embed: IndexEmbed | EmbedConfig | dict, *,
>     tags=None, deletion_protection='disabled', read_capacity=None, schema=None, timeout=None,
> ) -> IndexModel
> ```
>
> **Correction to the note above, verified 2026-09-29.** Two claims in the earlier version of
> this entry were wrong in ways that mattered, and both were found by executing the calls
> rather than by reading them.
>
> **`create_index_for_model` is not absent — `create_for_model` is.** Introspection had
> reported `hasattr(Pinecone, 'create_for_model') -> False` and concluded the method did not
> exist. It does not exist *on `Pinecone`*; it exists on `Pinecone.indexes`. The two classes
> expose different surfaces, and a negative `hasattr` on one says nothing about the other.
> The real surface is:
>
> ```
> dir(Pinecone.indexes)  -> ['configure','create','create_backup','create_for_model',
>                            'delete','describe','describe_backup','exists','list',
>                            'list_backups']
> ```
>
> **`IntegratedSpec` is present but the 9.x route through it raises.** `hasattr` is true for
> the class, and the class imports cleanly, so the earlier "not deprecated" reading was an
> artefact of checking presence rather than availability. Executed against a dummy key — the
> guard fires before any HTTP request, so this needed no network:
>
> ```
> pc.indexes.create(name="x", spec=IntegratedSpec(cloud="aws", region="us-east-1", embed=EmbedConfig(...)))
> -> pinecone.errors.exceptions.PineconeTypeError:
>    Indexes.create() no longer accepts spec=IntegratedSpec(...) — the 2026-07 API
>    creates integrated-embedding indexes through create_for_model instead: ...
> ```
>
> The rejection message is itself the migration note, naming `create_for_model` with the exact
> keyword form. `Pinecone.create_index` remains as a documented backwards-compatibility shim
> forwarding to `Indexes.create`, so it fails identically and is not an escape route.
>
> **Four `Pinecone`-level shims are therefore deliberately avoided** in `vector_store.py`, in
> favour of the `indexes` methods that each shim forwards to: `indexes.exists` (not
> `has_index`), `indexes.create_for_model` (not `create_index_for_model`),
> `indexes.describe` (not `describe_index`), and no `Pinecone.Index` constructor. This is not
> an aesthetic preference — the shims are documented as migration aids with a narrower surface,
> and R-008's ownership of timeouts is easier to honour against the real method.
>
> Further details from the same introspection, all load-bearing:
>
> | Question | Answer from `pinecone==10.0.0` |
> |---|---|
> | `IndexEmbed` fields | `model`, `field_map`, `metric`, `read_parameters`, `write_parameters` |
> | `EmbedConfig` fields | `model`, `field_map`, **`dimension`**, `metric`, `read_parameters`, `write_parameters` |
> | `create_index` (manual path) | `(name, spec, dimension, metric, vector_type, …)` — the caller supplies `dimension` and `metric`, which is precisely what Principle VII forbids |
> | `Pinecone.has_index` | `(name: str) -> bool` — the cheapest truthful readiness probe, and what `routes_health` uses |
> | `Index.search` | keyword-only: `(namespace, top_k, inputs, vector, id, filter, fields, rerank, match_terms, query, timeout)` — there is no `include_values`; returned fields are selected with `fields=` |
> | `Inference.rerank` | `(model, query, documents, rank_fields=['text'], return_documents=True, top_n=None, parameters=None) -> RerankResult` |
> | `Inference.embed` | `(model, inputs, parameters=None) -> EmbeddingsList` |
> | `RerankModel` members | `Bge_Reranker_V2_M3`, `Cohere_Rerank_3_5`, `Pinecone_Rerank_V0` — so `bge-reranker-v2-m3` (R-004) is a valid name in this version |
>
> **Why `create_index_for_model` is the correct answer on the merits, not merely the available
> one.** `create_index` requires the caller to state `dimension` and `metric`; the integrated
> path lets Pinecone derive both from the named model. The contract's `index.dimension_source`
> is `const: 'service'` — the claim is that dimensionality is read from service configuration
> and never hard-coded. On the `create_index` path this codebase would have to *assert* that
> claim; on the `create_index_for_model` path the service sets the dimension and the
> application never holds an opinion about it, so the claim is true by construction.
>
> **What this note does NOT resolve.** The `input_type` question above remains open, and it is
> the job of T027's *execution*. Whether an over-length chunk is silently truncated or rejected
> is a property of the hosted service, not of the SDK, and Pinecone's own documentation says a
> mismatch "quietly degrades" search quality while both calls succeed. That cannot be
> introspected and must be measured against a live index. R-001's
> `CHUNK_TARGET_MAX_TOKENS=1000` against the model's 2048-token ceiling is what keeps chunk
> inputs out of the danger range; the smoke test is what proves the ceiling is real. Until
> T027 has been *run*, the input-type behaviour is unverified and no code path may assume it.

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
top-6 be configured and tested independently (FR-019, FR-020).

```python
rr = pc.inference.rerank(
    model="bge-reranker-v2-m3",
    query=original_query,
    documents=[c.text for c in candidates],
    top_n=6,
    return_documents=False,
)
# rr.data[i].score normalised to [0,1]; higher is more relevant
```

> **Amendment, 2026-09-29.** R-004 originally specified `top_n=5`. That was
> arithmetically unsatisfiable: evidence is selected from the reranked set, and
> FR-020 requires three to six evidence units, so a 5-wide rerank cannot produce six.
> The budget is now 12 → 6 → 3–6, with `top_n=6`. The stage-independence argument above
> is unaffected — it is about not fusing two stages, not about the number. `config.py`
> now refuses at startup to start if `evidence_max_units` exceeds
> `retrieval_rerank_top_n`, so this class of contradiction is a startup error rather
> than a silently capped evidence set.

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

### Amendment 2026-09-30 — measured against real pages from the manifest

R-009 above was written from fixtures. Running the walk against live
`support.atlassian.com`, `confluence.atlassian.com` and `developer.atlassian.com`
pages produced four findings the fixtures could not have shown. All four are
recorded because each one changes code, and two of them are invisible in output.

**1. The walk is correct; the pages are shallower than assumed.** Measured across
14 real Jira documentation pages, the heading depth inside `<main>` is:

| Deepest level | Pages |
|---|---|
| 2 | 9 of 14 |
| 3 | 4 of 14 |
| 4 | 1 of 14 |

Nothing deeper than level 4 anywhere in the sample, and most pages are
`h1` + a flat run of `h2`. On `reopen-a-sprint` the walk recovers
`Reopen a sprint > Scenarios for reopening sprints > Simple scenarios` and
truncates correctly to `… > Complex scenarios` on the sibling — which is the
behaviour the whole note exists to specify, working on a real page. A system
expecting deep nesting everywhere would be waiting for structure these pages do
not have; the walk handles depth 1 and depth 4 identically.

**2. `h1` text is frequently repeated as the first `h2`.** Measured on
`access-a-project`: `<h1>Access a space</h1>` then `<h2>Access a space</h2>`.
Carried literally, every unit beneath gets `["Access a space", "Access a
space"]` — a path that reads as a section nested inside itself and names no real
location. `_push_heading` skips a level whose text repeats its parent. This is
*not* heading de-duplication: it applies only across differing levels, so two
distinct `h2` sections sharing a name are unaffected, and the level structure the
stack depends on is preserved.

**3. Two of six manifest start_urls are landing pages, not articles.**
`create-and-organize-work-in-confluence-cloud` and `developer.atlassian.com/cloud/jira/platform`
have 2 and 6 raw `<hN>` elements respectively and yield **zero** headings after
extraction — verified that the content *is* server-rendered (13,048 and 3,846
words in the raw HTML, no empty-container shell signature), so trafilatura is
selecting correctly and these are genuinely one-`h1` landing pages. `favor_recall`
and `favor_precision` produce byte-identical output on both, so no option fixes
this because nothing is broken. **Consequence for the manifest**: six sources at
one URL each is not enough to produce a corpus with `h2`+ structure, and T017's
topic expectations assume a hierarchy these six pages largely do not have. The
crawl scope rule (host + containing directory) is what must widen the corpus,
not the extractor.

**4. trafilatura drops all heading structure on short pages — measured threshold
≈ 3 sentences.** Below it, a page with `<h1>` and `<h2>` returns **one
concatenated `<p>`** with the headings flattened *into the prose as text*, not
omitted. This is a different behaviour from "this page has no headings" and
must not be read as one; a pipeline that trusted it would index the boilerplate
and a site's own navigation under `heading_path == []`. This is the concrete
case T051's "main content too small" rejection (FR-029) exists to exclude, and
`test_a_page_too_short_for_extraction_yields_no_heading_path` pins it.

**Not fixed here, and deliberately.** Real pages also yield interactive
boilerplate as content blocks — `"Was this helpful?"` and `"Rate this page:"`
both arrive as ordinary `<p>` units and are currently treated as indexable
content. That is *block-level* filtering, which is a different job from T051's
*page-level* rejection ("main content is too small or too boilerplate-heavy",
FR-029), and it belongs there rather than being pre-empted here. Recorded so it
is a decision to make at T051 and not a surprise at the Phase 3 checkpoint.

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

## R-014: Model reasoning is received and never exposed — FR-034 discharged at the boundary

**Decision**: read exactly one field out of the provider's message — `content` — and build a
`Completion` that has no field capable of carrying anything else. Everything else the
provider sends is discarded at the adapter boundary. A post-condition walks the constructed
value on every return, and a text heuristic catches deliberation inlined into `content`.

**Revised 2026-09-29 by measurement.** The original decision for this finding was to
"explicitly disable reasoning exposure on every provider call" using `include_reasoning=False`
and `reasoning_format="hidden"`. That is **wrong on this surface, and had it been implemented
would have failed on the first query.** The controls named do not exist on Groq's
OpenAI-compatible endpoint. Measured against a live account on 2026-09-29:

| Probe | Result |
|---|---|
| `reasoning_effort="none"` | HTTP 400 `` `reasoning_effort` must be one of `low`, `medium`, or `high` `` |
| `openai/gpt-oss-120b`, effort `low` | `message` keys `[content, reasoning, role]`; `reasoning` 23 chars; `reasoning_tokens` 5 |
| `openai/gpt-oss-120b`, effort `high` | `message` keys `[content, reasoning, role]`; `reasoning` 578 chars; `reasoning_tokens` 124 |
| `openai/gpt-oss-20b`, effort `low` | `message` keys `[content, reasoning, role]`; `reasoning` 18 chars; `reasoning_tokens` 5 |
| `openai/gpt-oss-20b`, effort `high` | `message` keys `[content, reasoning, role]`; `reasoning` 407 chars; `reasoning_tokens` 94 |

Three conclusions, and the first is the one that changed the implementation:

1. **There is no parameter that turns the channel off.** `none` is rejected, and every
   accepted effort returns a populated `message.reasoning`. The only lever is `low`, which
   reduces the tokens spent on deliberation (5 vs 124 on the 120B) without removing it.
   So the control cannot be a request parameter.
2. **FR-034 is about exposure, not receipt.** It says the system MUST NOT *expose* private
   model reasoning — not that it must not receive it. A provider reasoning is permitted; a
   reader of this system seeing it is not. The obligation falls on the emitted surface, which
   is the one place the system controls.
3. **An earlier reading of this finding would have been catastrophic, not merely strict.**
   Refusing any response containing a reasoning key — the shape the original decision implies
   — fails on **every** query from a reasoning model, because both configured models reason
   by default. That is a system that cannot answer anything, defended under the banner of
   protecting a requirement. The `assert_no_reasoning` check is retained, retargeted at the
   value *about to be returned* rather than the provider's envelope.

**Why reading one field rather than filtering keys.** The obvious alternative is walking the
response and dropping anything matching `REASONING_KEY_PATTERN`. That is denylist-shaped, so it
stays correct for responses that do not reason and silently passes on the next novel key name
a provider invents. `Completion` is built from `content` alone, which has no equivalent
failure. The post-condition then walks dataclass fields, so a future `Completion.reasoning` is
a run-time failure rather than a new field that satisfies every type checker and no requirement.

**Two checks, because they catch different things.** The structural check proves no field exists
that could carry deliberation; it cannot prove the *content* is free of it, since a model can
inline "First, let us consider…" where no key betrays it. `text_looks_like_reasoning` is the
second check and is documented as the heuristic it is: a false positive refuses an answer that
was fine, which is the correct direction for a MUST NOT to fail in.

**What is recorded about the discard.** The boundary reports *that* a channel was dropped and
how many `reasoning_tokens` it cost — never the text, in a log or an exception, because a quoted
leak has only moved rooms. A silent discard is otherwise indistinguishable from a service that
stopped reasoning, and those two have different causes and different costs.

**`reasoning_effort` is therefore a cost control, not a compliance control.** It is set to
`low` on all calls, `Literal["low", "medium", "high"]` in settings so an unsupported value
fails at startup rather than as a 400 on the first real query, and R-014's original latency
note about SC-017 still holds: at `high` the 120B spends 124 tokens deliberating a one-sentence
answer, and that spend buys nothing when the result is discarded.

**Contract test**: the event schemas in [contracts/events.md](./contracts/events.md) contain no
reasoning, thought, or deliberation field. The sole reasoning-named field anywhere is
`reasoning_exposed: false` on `GET /config`, which is the attestation that this decision is in
force — `tests/contract/test_openapi_conformance.py` asserts it exists, is constant `False`, and
is the only such name.

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
