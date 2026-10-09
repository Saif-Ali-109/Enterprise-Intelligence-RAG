# Architecture

An evidence-grounded RAG system over publicly available Atlassian documentation. It ingests
pages by URL at run time, answers only from retrieved evidence, cites every substantive claim
with a link back to the original page, and refuses when the evidence is thin. Citations are
validated in code, never trusted to the model.

This document explains how the system is put together and why it is put together that way.
The constraints it works under are the ratified constitution in
[`.specify/memory/constitution.md`](../.specify/memory/constitution.md); the requirements are in
the [specification](../specs/001-enterprise-knowledge-rag/spec.md).

---

## 1. The shape of a question

Every request follows one path. There is no second path, no fallback search, and no partial
mode — a reviewer can read the pipeline top to bottom and know exactly what a given answer did.

```
                       ┌──────────────┐
   POST /chat/stream → │  analyze     │  question → product, category, intent, rewrites
   POST /chat         │              │  ambiguity detected from the question's own words
                       └──────┬───────┘
                              │ QueryAnalysis (typed, no dicts)
                              ▼
                       ┌──────────────┐
                       │  retrieve    │  Pinecone search, 12 candidates per query
                       │              │  metadata filter → plan → widen once if empty
                       └──────┬───────┘
                              │ Candidate[] (provenance joined from the registry)
                              ▼
                       ┌──────────────┐
                       │  rerank      │  Pinecone rerank, truncate=END, top 6
                       └──────┬───────┘
                              │ RerankedHit[]
                              ▼
                       ┌──────────────┐
                       │  select      │  3–6 evidence units, ≤2 per document,
                       │              │  ≥2 products when the question spans them
                       └──────┬───────┘
                              │ EvidenceSource[]
                              ▼
                       ┌──────────────┐
                       │  quality gate│  refuse below the configured score floors
                       └──────┬───────┘
                              │ (refusal path ends here, honestly)
                              ▼
                       ┌──────────────┐
                       │  generate    │  Groq, own-JSON contract, ≤2 attempts,
                       │              │  reasoning discarded at the adapter boundary
                       └──────┬───────┘
                              │ GeneratedAnswer
                              ▼
                       ┌──────────────┐
                       │  validate    │  every citation resolved against served
                       │              │  evidence; URLs looked up from `documents`,
                       │              │  never from model output
                       └──────┬───────┘
                              │ ValidatedCitation[]
                              ▼
                       ┌──────────────┐
                       │  markers     │  [E1] → rank; unserved marker → nothing
                       └──────┬───────┘
                              ▼
                    answer + citations, or an explicit refusal
```

**The gates are not decoration.** `select_evidence` and `gate_evidence` run before generation,
so most questions on a thin corpus never reach a model at all. In the measured run
[`dfa3a1fd`](EVALUATION.md), 31 of 32 gold-set questions refused — the gate working as designed
on a corpus that cannot support them.

### Streaming is the same pipeline

`POST /chat/stream` calls the identical `run_chat` function the JSON endpoint calls. The only
difference is transport: an `EventEmitter` receives eight ordered frames, and
[`contracts/events.md`](../specs/001-enterprise-knowledge-rag/contracts/events.md) is normative
for their order, their fields, and their termination. Because the pipeline is shared, a streamed
answer cannot be a different answer — and the contract suite asserts the two paths agree rather
than trusting that.

---

## 2. Modules

```
backend/app/
├── api/            HTTP surface: routes, error envelopes, SSE, event vocabulary
├── chat/           the pipeline itself: service.py, audit.py, marker resolution
├── retrieval/      query analysis, rewriting, filtering, search, reranking, evidence, citations
├── generation/     provider adapter, prompts, generator, verifier
├── ingestion/      fetching, robots, extraction, chunking, embedding, upsert, crawl orchestration
├── evaluation/     gold-set loader, metric definitions, run orchestrator, gate evaluation
├── db/             SQLAlchemy models, session, migrations
├── core/           config, errors, logging, sanitising, security, tokens, rate limiting
└── schemas/        Pydantic request/response models in the contract's vocabulary
```

Roughly 17,900 lines of application code against 17,500 lines of tests. The ratio is not a
target; it is a consequence of writing the test first for each story, which the task list
enforces by listing tests before implementations.

### Why a modular monolith

Constitution VIII prohibits unnecessary microservices and requires the RAG mechanics to stay
legible. Splitting this into services would mean a network hop between retrieval and reranking
and a second deployment to reason about, in exchange for scale this workload does not have —
the corpus is 50–100 pages. What the boundary discipline *does* buy is testability: each stage
takes typed inputs and is exercised in isolation, which is why
`tests/integration/test_metrics_respond.py` can swap the entire provider for a stub and watch
five metrics move.

### Dependencies are injected at the boundaries

`run_chat(question, *, session, store, provider, reranker=None, inspect=False, emit=None)` takes
its collaborators as arguments. That is what lets the tests replace Pinecone with a store that
raises `VectorServiceUnavailable`, replace Groq with a refusing stub, and replace the emitter
with nothing at all — none of which require patching a global.

### Vendor access sits behind one adapter each

`retrieval/vector_store.py` and `generation/provider.py` are the only modules that import
Pinecone and Groq. Constitution VIII permits thin adapters and forbids excessive abstraction;
these are thin. Nothing above them knows a vendor exists, and nothing inside them knows what a
citation is.

---

## 3. Decisions that shaped the code

**Provenance is joined from the registry, never read from search hits.** Pinecone's
`Index.search` returns `metadata=None` for every `fields` value (measured on `pinecone==10.0.0`),
so provenance comes from `DocumentUnit` → `Document` in Postgres. The consequence is visible in
`retriever.retrieve`: every candidate is hydrated with product, category, heading path, and token
count before reranking, because a citation that cannot name its source is not a citation.

**Vector ids are immutable.** `{document_id}#{ordinal:04d}`. A re-crawl that produces identical
units upserts the same ids; a page that shrinks supersedes the ids past its new end and
tombstones the rest. This is why V3 ("re-crawling adds no duplicates") needs no special case.

**The classification vocabulary is an enum, not a string.** `DocumentCategory` and `PageType` live
in `core/config.py`. The analyzer's prompt names them, the extractor emits them, the retrieval
filter consumes them, and anything outside the vocabulary becomes `unknown`. A single enum is what
makes those three agree; free strings made them drift.

**Retrieval budget is configuration, validated at construction.** 12 candidates → 6 reranked → 3–6
evidence units, checked in the settings validator so an inconsistent budget is a startup failure
rather than a wrong answer.

**Reranking sends `truncate=END`.** Measured: the default pair limit raises a 400 on a
600–1000-token pair. The parameter is not a tuning preference; it is what makes the request valid.

**Generation does not use `json_mode`.** Measured: Groq returns `400 json_validate_failed`. The
generator parses its own JSON contract instead, with a bounded retry (`GENERATION_MAX_ATTEMPTS=2`,
`GENERATION_RETRY_DELAY_SECONDS=1.5`) and an honest refusal if the second attempt also fails.

**Reasoning is discarded at the adapter boundary.** If the provider returns a reasoning channel,
it is dropped before anything else sees it and the drop is logged as a fact
(`carried_reasoning: true`, token count) — the evidence that FR-034's control ran. No reasoning
content reaches a payload, an event, or a stored record.

**Citations are validated deterministically.** `retrieval/citations.py` compares every
model-claimed identifier against the evidence actually served and resolves every URL from the
`documents` table. A model that names a plausible URL gets the URL that page really has, or loses
the citation.

**Empty beats zero.** Unset score floors, not-applicable retrieval metrics, a run with no
measurements, a corpus with no documents: each renders as "not measured" rather than as `0`,
because `0` is a claim and a missing value is not. This rule runs through the API schemas, the
frontend, and the metric definitions.

---

## 4. Storage, and what is deliberately not stored

Nine tables in Postgres (`data-model.md` §1). The registry holds provenance and structure; the
vector store holds text. **Page text is never written to this application's database.**

The single exception is `citations.quote`: the bounded span that supports a claim, stored so a
reviewer can check the claim without re-fetching the page. Nothing else retains third-party text,
and no page content is committed to Git (T173, V14).

The four request-scoped pipeline artifacts — analysed question, retrieved candidates, reranked
candidates, selected evidence — are snapshotted into `query_logs.pipeline_trace` as one JSON
document, and **only when `inspect: true`**. Promoting them to tables would need a stated
relational query that a JSON document cannot serve; none exists today.

---

## 5. Request lifecycle and observability

Every request carries one id from the middleware to the error envelope, the log lines, and the
`query_logs` row (`get_request_id()` in the service, FR-047). Minting a second id inside the
service was tried and reverted: it produced a split where a quoted request id found no audit row.

Errors use one envelope: a code, a safe message, a request id, and optional details. Stack traces
and internal paths never appear in a response or a log. `query_logs` records the counts that make
a complaint investigable — candidates retrieved, reranked, evidence selected, per-stage latency,
provider, model, token usage — and per-question citation rows record which passages were served.

Two operational facts worth stating plainly:

- **A test run truncates the development database.** The integration suite runs against the same
  Postgres the application uses, so re-register sources after `make test-all`.
- **Logging "provider reasoning discarded" is deliberate.** It reports that a channel arrived and
  was dropped, which is how FR-034 is verified rather than assumed.

---

## 6. The frontend

Next.js App Router, React 19, Tailwind 4, TanStack Query. Five views: ask (`/`), sources
(`/sources`), documents (`/documents`), a document's detail and units (`/documents/[id]`), and
evaluation (`/evaluations`), with configuration and health on `/settings`.

The browser talks only to same-origin `/api/*` routes. `app/api/[...path]/route.ts` forwards
`/api/v1/*` to the backend and `app/api/chat/stream/route.ts` relays the SSE stream unchanged —
`BACKEND_BASE_URL` is resolved server-side, so the backend's URL and its credentials never reach
the client bundle, and no CORS configuration can be got wrong.

The chat panel consumes `/chat/stream` directly through `lib/sse.ts`. `EventSource` cannot issue a
POST, so the reader is `fetch` plus a `ReadableStream`: it buffers until a frame's blank line
arrives, keeps decoder state across reads, joins multi-line `data:`, surfaces a non-JSON payload
as raw text rather than throwing, and stops at the terminal frame.

Accessibility is structural rather than decorative: streamed progress reaches assistive technology
through two persistent live regions (a polite one for stage announcements, an assertive one for the
outcome — never one region whose politeness flips), and the announcement set is exactly the set
`events.md` §8 defines.

---

## 7. Where to look when something is wrong

| Symptom | First place to look |
|---|---|
| An answer is uncited or wrong | `query_logs.pipeline_trace` for that request id (`inspect: true`) |
| Retrieval found nothing useful | `searched.applied_filters` and `queries_executed` in the same row |
| A refusal you expected to be an answer | the quality gate: score floors are unset by default |
| A citation points at the wrong section | `citations` rows for the query — `validation_state` and `validation_note` |
| The corpus is not what you expect | `/documents` filters, and `state_detail` on tombstoned pages |
| Quality metrics moved | `evaluation_runs.config_snapshot` — a configuration change is the usual cause |

With `inspect: true`, the trace answers "where did it stop?" for any request: eight stages with
counts, the queries run, the filters applied, whether the scope widened, and the per-stage
latency. It is pipeline state only — there is no reasoning field to render.

---

## 8. Related documents

- [Data source policy](DATA_SOURCE_POLICY.md) — what is collected, what never is, crawling conduct.
- [Evaluation guide](EVALUATION.md) — running the suite, reading a failing gate.
- [API reference](API.md) — generated from the contract, which is normative.
- [Quickstart](../specs/001-enterprise-knowledge-rag/quickstart.md) — the fourteen validation
  scenarios a reviewer runs.