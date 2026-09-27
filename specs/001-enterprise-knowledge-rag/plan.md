# Implementation Plan: Enterprise Knowledge Intelligence RAG

**Branch**: `001-enterprise-knowledge-rag` | **Date**: 2026-09-27 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `specs/001-enterprise-knowledge-rag/spec.md`

## Summary

Build an evidence-grounded enterprise knowledge assistant over publicly accessible
Atlassian documentation (Jira, Confluence, Jira Service Management, Developer Platform),
ingesting by URL rather than shipping a corpus.

**Primary requirement**: answer questions with every substantive claim cited to a
retrievable source, refuse when evidence is thin, expose the deterministic retrieval
pipeline without chain-of-thought, and measure quality honestly from real evaluation runs.

**Technical approach** (detail and rationale in [research.md](./research.md)): a modular
Python monolith with a typed, independently testable stage per pipeline step — respectful
crawler → main-content extraction preserving heading structure → structure-aware chunking
→ hosted-embedding vector store with metadata filtering → query analysis and expansion →
dense retrieval → reranking → evidence selection → grounded generation with deterministic
citation validation → evaluation. PostgreSQL holds registry, audit, and evaluation state;
the vector store holds vectors only. Next.js dashboard over SSE.

## Technical Context

**Language/Version**: Python 3.12+ (backend); TypeScript 5.x (frontend)

**Primary Dependencies**: FastAPI, Pydantic v2, SQLAlchemy 2.x, Alembic, asyncpg, httpx,
lxml, trafilatura, Pinecone Python SDK, groq SDK, pytest; Next.js 16, React 19, Tailwind CSS
4, shadcn/ui, TanStack Query 5. **Not** BeautifulSoup, **not** selectolax, **not** a RAG
orchestration framework, **not** a tokenizer library — each exclusion is justified in
[research.md](./research.md) (R-010, R-013) and the framework exclusion in Complexity
Tracking below.

**Storage**: PostgreSQL 16 (registry, audit, evaluation state, gold dataset) + Pinecone
serverless index (vectors only, integrated embedding). PostgreSQL is never used as a vector
store. **Retrieval is never degraded to a substitute**: no keyword-only fallback, no cached
prior answer (FR-062).

**Testing**: pytest (unit, integration, end-to-end); Playwright or equivalent for the
accessibility and keyboard-navigation gates

**Target Platform**: Linux server, Docker Compose; browser-based dashboard

**Project Type**: web-service (API backend + browser dashboard)

**Performance Goals**: first progress update within 2s; complete answer with citations
within 8s for 95% of questions; no unexplained pause over 10s

**Constraints**: corpus bounded to 50–100 pages / 500–1500 chunks; one active crawl per
source; retrieval pool 12 → rerank 5 → final context 3–6; max 2 generation attempts;
crawl bounded by page count, depth, and inter-request delay; SSRF allowlist on all fetches

**Scale/Scope**: single-operator research and demonstration tool. No end-user accounts, no
multi-tenancy (namespace structure is forward-compatible only), no write-back to external
systems, no non-English corpora, no live-data retrieval. Modular monolith — explicitly
**not** microservices (Constitution Principle XI).

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Principle / Constraint | Status | Evidence in plan |
|---|---|---|
| I. Grounded Answers, Deterministic Citations | PASS | Citation validation is a separate deterministic module with its own contract and unit tests; citation IDs originate from retrieved context only |
| II. Refuse Rather Than Guess | PASS | Thresholds configurable; `null` classification honoured; ≤2 generation attempts; refusal is a first-class response type, not an error path |
| III. Structure-Aware Ingestion and Traceability | PASS | 11 discrete ingestion stages, each independently testable; heading-path preserved per unit; content-hash versioning; explicit 6-state document lifecycle |
| IV. Corpus Independence and Legal Compliance | PASS | Ingest-by-URL only; robots.txt respected; allowlist + delay + caps; no scraped content in Git; delete removes vectors **and** registry; disclaimer in README and UI |
| V. Explainability Without Chain-of-Thought | PASS | Pipeline metadata contract; developer mode exposes stages only; no reasoning fields in any event or response schema |
| VI. Honest Evaluation, No Fabricated Metrics | PASS | Metrics computed per run, timestamped, dataset-identified; empty state when no run; threshold pass/fail reported explicitly (FR-041) |
| VII. Provider-Neutral Interfaces, Env-Driven Config | PASS | `LLMProvider` and `Reranker` interfaces; Groq default behind OpenAI-compatible interface; index/namespace/embed model/thresholds all env-driven; dimension never hard-coded |
| VIII. Small Services and Clear Boundaries | PASS | One module per pipeline stage with typed boundaries and DI; vendor calls (Pinecone, Groq) isolated in adapter modules, not buried in logic |
| IX. Security by Default | PASS | Env-only secrets; SSRF deny-list checked pre-connection; CORS explicit; length/size caps; HTML sanitisation; structured error envelope, no stack traces |
| X. Observable, Secret-Safe Operation | PASS | Request id propagated end-to-end; per-stage latency; provider/model/token usage logged; secrets excluded; bounded retention on stored questions |
| XI. Deliberate Scope, Incremental Delivery | PASS | 50–100 page cap honoured; 20-phase order preserved; no microservices; cloud vector service not replaced with a local fake |
| Additional: retrieval budget | PASS | 12 → 5 → 3–6 enforced as configuration, not convention |
| Additional: metadata discipline | PASS | Fixed compact metadata schema in [data-model.md](./data-model.md) |
| Additional: interface surface | PASS | Endpoint contract in [contracts/](./contracts/) |

**Gate result: PASS.** No violations requiring a Complexity Tracking entry.

**Compliance reviews applied at design time**
- *Constitution VIII (justify added complexity)*: chose one relational store plus one
  vector store over a document/graph store hybrid. Postgres carries registry + audit +
  evaluation; adding a graph store would duplicate provenance already modelled by
  `content_hash` and `heading_path`, and is rejected under Principle XI.
- *Constitution VII (justify new dependencies)*: any new runtime dependency must state the
  capability it buys and why the standard library or an existing dependency is
  insufficient. Recorded per-dependency in [research.md](./research.md).
- *Constitution III (do not hide vendor operations)*: Pinecone and Groq calls live in
  adapter modules with thin interfaces. Retrieval logic depends on the interface, never on
  the SDK types — but adapters are not wrapped in extra generic layers.

## Project Structure

### Documentation (this feature)

```text
specs/[###-feature]/
├── plan.md              # This file (/speckit.plan command output)
├── research.md          # Phase 0 output (/speckit.plan command)
├── data-model.md        # Phase 1 output (/speckit.plan command)
├── quickstart.md        # Phase 1 output (/speckit.plan command)
├── contracts/           # Phase 1 output (/speckit.plan command)
└── tasks.md             # Phase 2 output (/speckit.tasks command - NOT created by /speckit.plan)
```

### Source Code (repository root)
<!--
  ACTION REQUIRED: Replace the placeholder tree below with the concrete layout
  for this feature. Delete unused options and expand the chosen structure with
  real paths (e.g., apps/admin, packages/something). The delivered plan must
  not include Option labels.
-->

```text
# Repository root
├── backend/
│   ├── app/
│   │   ├── main.py                  # FastAPI app factory, middleware, router wiring
│   │   ├── core/
│   │   │   ├── config.py            # env-driven settings (pydantic-settings)
│   │   │   ├── logging.py           # structured logging, request-id binding
│   │   │   ├── security.py          # SSRF guard, URL validation, input caps
│   │   │   └── errors.py            # error envelope + exception handlers
│   │   ├── db/
│   │   │   ├── models.py            # SQLAlchemy 2.x ORM models
│   │   │   ├── session.py           # async engine/session factory
│   │   │   └── migrations/          # Alembic
│   │   ├── schemas/                 # Pydantic v2 request/response models
│   │   ├── ingestion/
│   │   │   ├── fetcher.py           # stage 1: HTTP + SSRF + robots
│   │   │   ├── extractor.py         # stage 3: main content + heading structure
│   │   │   ├── cleaner.py           # stage 4: normalisation
│   │   │   ├── metadata.py          # stage 5: title/product/category/lang
│   │   │   ├── chunker.py           # stage 7: structure-aware split
│   │   │   └── pipeline.py          # stage orchestration
│   │   ├── retrieval/
│   │   │   ├── query_analyzer.py    # product/category/intent/entities + confidence
│   │   │   ├── query_rewriter.py    # multi-query expansion
│   │   │   ├── vector_store.py      # Pinecone adapter (interface + impl)
│   │   │   ├── retriever.py         # dense retrieval + metadata filters
│   │   │   ├── reranker.py          # Reranker interface + hosted impl
│   │   │   ├── evidence.py          # evidence selection / diversity
│   │   │   └── citations.py         # deterministic citation validator
│   │   ├── generation/
│   │   │   ├── provider.py          # LLMProvider interface + Groq impl
│   │   │   ├── prompts.py           # system prompt, evidence serialisation
│   │   │   ├── generator.py         # grounded answer generation
│   │   │   └── verifier.py          # SUPPORTED/UNSUPPORTED/PARTIAL
│   │   ├── evaluation/
│   │   │   ├── dataset.py           # gold set loader
│   │   │   ├── metrics.py           # recall/precision/MRR/nDCG/faithfulness
│   │   │   └── runner.py            # run orchestration + threshold gates
│   │   └── api/
│   │       ├── routes_chat.py       # + SSE streaming
│   │       ├── routes_sources.py
│   │       ├── routes_documents.py
│   │       ├── routes_evaluations.py
│   │       └── routes_health.py
│   ├── tests/
│   │   ├── unit/                    # chunker, security, citations, analyzer…
│   │   ├── integration/             # postgres, vector store, pipelines
│   │   └── e2e/                     # question → answer → validated citations
│   ├── requirements.txt
│   └── Dockerfile
│
├── frontend/
│   ├── app/                         # App Router: /, /sources, /documents,
│   │   │                            #   /evaluations, /settings
│   │   └── api/chat/stream/route.ts # transparent SSE proxy (POST)
│   ├── components/                  # chat, evidence panel, source cards,
│   │                                #   inspection trace, eval dashboard
│   ├── lib/
│   │   ├── sse.ts                   # fetch + ReadableStream reader (EventSource can't POST)
│   │   ├── api-client.ts
│   │   └── live-region.tsx          # aria-live announcement of progress (FR-060)
│   ├── hooks/
│   ├── types/
│   ├── e2e/                         # Playwright: keyboard nav + axe-core pass
│   └── Dockerfile
│
├── data/
│   └── source_manifest.json         # source URLs only — never page content
├── evaluation/
│   └── golden_questions.json        # 30+ topic-graded questions
├── docs/
│   ├── ARCHITECTURE.md
│   ├── DATA_SOURCE_POLICY.md
│   ├── EVALUATION.md
│   └── API.md                       # published interface reference
├── scripts/
│   ├── validate_datasets.py         # JSON Schema check on the two committed files
│   └── check_repo_hygiene.sh        # SC-015: no secrets, no publisher prose in Git
├── docker-compose.yml               # backend, frontend, postgres
├── .env.example
├── .gitignore
├── README.md
└── LICENSE                          # MIT
```

**Structure Decision**: Web application layout (backend + frontend) — the feature is
detected as "frontend" + "backend" from the spec's user stories. The backend is a
**modular monolith**: one deployable unit, one package per pipeline stage, no inter-service
network calls. This satisfies Constitution Principle VIII (small services, clear
boundaries) without violating Principle XI (no unnecessary microservices).

**`contracts/` exists only under `specs/001-enterprise-knowledge-rag/`, not at the repository
root.** It is a design artifact. The published interface reference is `docs/API.md`, and the
implementation conforms to the contract rather than duplicating it. The two committed data
files are machine-validated against their schemas by `scripts/validate_datasets.py`, so the
guarantees the schemas encode — no URL pinning in the dataset, no page content in the
manifest — are enforced on every change rather than merely documented.

Deliberate choices:
- `ingestion/`, `retrieval/`, `generation/`, `evaluation/` mirror the spec's pipeline
  stages 1:1, so a reviewer can trace requirement → module.
- `vector_store.py` and `provider.py` are the only modules permitted to import vendor
  SDKs. Everything else depends on their interfaces.
- `security.py` sits in `core/` because SSRF guards are needed by both the crawler and the
  source-registration endpoint.
- `routes_chat.py` owns both the streaming and non-streaming entry points, which execute the
  identical pipeline — the evaluation runner uses the non-streaming form, so measured metrics
  reflect the same work a user sees (R-007).
- `live-region.tsx` is separated because FR-060 is a contract requirement, not a styling
  detail: which events are announced is specified in
  [contracts/events.md](./contracts/events.md) §8.

## Complexity Tracking

> **Fill ONLY if Constitution Check has violations that must be justified**

**No violations.** The Constitution Check passed with no unjustified complexity, so this
table is intentionally empty.

Complexity was actively *removed* during planning:

| Candidate complexity | Decision | Rationale |
|---|---|---|
| Microservices per pipeline stage | Rejected | Principle XI. Cross-stage calls would be network calls in one process, adding failure modes and latency for no isolation benefit |
| Graph store for doc relationships | Rejected | Provenance is already fully modelled by `content_hash` + `heading_path`; a graph store would duplicate it |
| LangChain / LlamaIndex for the pipeline | Rejected | Constitution VIII forbids framework-by-default composition that obscures retrieval mechanics. A reviewer must be able to read the actual chunking and retrieval logic |
| Local vector store (e.g. Qdrant container) for offline dev | Rejected | Spec §54 and Principle XI forbid faking the vector service. Testing against a real hosted index is the point |
| Caching layer (Redis) | Deferred | Not in the mandated stack. No measured need at 50–100 pages / 500–1500 chunks |
| BeautifulSoup / selectolax in the extraction path | Rejected | R-010. trafilatura already returns lxml, so lxml means zero re-parsing; bs4 measured ~14x slower. A third parser would add a conversion step for no gain |
| A tokenizer dependency for chunk sizing | Rejected | R-013. The embedder is hosted and its tokenizer is not exposed, so any local count is an approximation either way. The margin is calibrated against a reference tokenizer in tests and is reversible on evidence |
| A local/vector-free dev mode | Rejected | Same as the local vector store above. A fallback retrieval path would make the refusal and groundedness gates unfalsifiable |
| Fused `search(rerank=…)` single round trip | Rejected | R-004. It couples two independently specified pipeline stages into one vendor call and makes the `Reranker` interface unswappable, which FR-019 requires |
| Token-delta streaming for the answer | Rejected | R-007. Not supported alongside structured output. Stage-based progress satisfies FR-035 and is where the wait actually is |
| A second `metadata` store inside the vector record | Rejected | R-003. Metadata is flat and filterable; nesting would break filter expressions |

### Deferred decisions

| Decision | Why deferred | Revisit when |
|---|---|---|
| `EmbedConfig` construction form (`create_for_model` vs `IntegratedSpec`) | Pinecone's own documentation is mid-migration and the two forms disagree. A v10 deprecation was reported but could not be confirmed from official sources (R-002) | Phase 3, resolved by a smoke test against the installed SDK — not by guessing |
| Rerank model entitlement (`bge-reranker-v2-m3` vs `cohere-rerank-3.5`) | Account-dependent; the `Reranker` interface is fixed either way | Phase 10 |
| Groq Developer-plan rate limits | Partially behind a client-rendered tab in the docs; figures came from a secondary table (R-008) | Phase 12, before capacity planning |

**No longer deferred** (decided 2026-09-28): embedding model `llama-text-embed-v2` (R-001);
language model pair `openai/gpt-oss-120b` / `openai/gpt-oss-20b` (R-006); nine-table
persistence model. Quality thresholds moved from fixed to configurable, defaulting to the
original figures, with the absolute zeros excluded (FR-065, FR-066). Threshold *values*
still require a real run to justify any change — that is a measurement question, not a
planning one, and it stays with Phase 18.

---

## Post-Design Constitution Re-Check

*Required by the Constitution Check gate: re-evaluated after Phase 1 design. Findings
below are the ones where the design **changed** the earlier assessment, not a restatement of
it.*

| Principle | Earlier | Post-design | What the design changed |
|---|---|---|---|
| I. Grounded Answers, Deterministic Citations | PASS | **PASS, strengthened** | The contract now makes registry lookup the only path to a citation URL (FR-003/FR-004), and R-001 removes a failure mode that would have produced citations to text the embedder never saw. R-014 removes a live FR-034 leak. Verified structurally: `AskResponse` and every event schema are closed to additional properties |
| II. Refuse Rather Than Guess | PASS | **PASS, strengthened** | Refusal is now a first-class `outcome` on the same response schema rather than a separate path, so a client cannot accidentally render it as an error. `VECTOR_SERVICE_UNAVAILABLE` is contractually barred from carrying any partial result (FR-062) |
| III. Structure-Aware Ingestion | PASS | **PASS, with a dependency** | trafilatura flattens the heading hierarchy (R-009), so FR-018 now has a hard implementation dependency on the custom reconstruction walk. This was not visible before research and is the single largest technical risk in the ingestion stage |
| IV. Corpus Independence and Legal Compliance | PASS | **PASS, strengthened** | The manifest schema has *no field in which page content could be recorded*, so FR-032 is enforced by the file format rather than by discipline. Verified: adding a content field fails validation |
| V. Explainability Without Chain-of-Thought | PASS | **PASS, with a new risk found** | The default models are reasoning models whose API can return raw reasoning unless explicitly suppressed (R-014). Three controls are now mandatory and one is a contract test. This was an unrecognised violation risk before research |
| VI. Honest Evaluation, No Fabricated Metrics | PASS | **PASS, strengthened** | `gate_outcome` is strictly `pass`/`fail` with no soft third value; `metrics` is `null` before a run rather than zero; nulls are documented as *not applicable* rather than *not measured*, which prevents a correct refusal from depressing a retrieval average. V12 in the quickstart exists specifically to catch metrics that fail to respond to a configuration change |
| VII. Provider-Neutral Interfaces | PASS | **PASS** | The `Reranker` split (R-004) and the hosted-model-only default (R-006) keep the interfaces honest. Model IDs are configuration only, and the one place a dimension could be hard-coded is explicitly omitted rather than set (R-002) |
| VIII. Small Services and Clear Boundaries | PASS | **PASS** | Two adapter boundaries confirmed as the only vendor-import sites. R-004's rejection of the fused call is what keeps the `Reranker` boundary real rather than nominal |
| IX. Security by Default | PASS | **PASS** | `/config` returns secret **presence** with no value field anywhere in the schema, and `reasoning_exposed` is a `const: false` field so conformance is checkable at runtime rather than by reading code |
| X. Observable, Secret-Safe Operation | PASS | **PASS** | `request_id` is the `query_logs` primary key, so the value a user quotes in a bug report *is* the audit record. Token usage and per-stage latency have defined fields; `total_time` is specified as the honest latency number because it includes queueing |
| XI. Deliberate Scope | PASS | **PASS** | Offset pagination chosen over cursors at a bounded corpus. The 30-question dataset minimum is enforced by schema, not convention |

**Post-design gate result: PASS.** No new violation was introduced, and three previously
unrecognised risks (I, III, V) were found and closed by design rather than deferred.

**One dependency worth stating plainly**: FR-018's heading-path requirement is only
satisfiable because of the custom walk in R-009. If that walk is wrong, the corpus view in
quickstart V2 is the test that catches it — units with flat or identical heading paths, or a
corpus with no `code` or `table` block types, mean the structure was lost.
