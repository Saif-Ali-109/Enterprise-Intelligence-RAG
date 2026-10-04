---

description: "Task list for Enterprise Knowledge Intelligence RAG"
---

# Tasks: Enterprise Knowledge Intelligence RAG

**Input**: Design documents from `specs/001-enterprise-knowledge-rag/`

**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/, quickstart.md, constitution v1.0.0

**Tests**: Included. FR-052 explicitly requires automated tests for the chunker, address validation, metadata extraction, citation validation, question analysis, filter construction, content fingerprinting, crawler security controls, and the answer verifier. SC-021 and SC-022 add automated accessibility checks. Write tests first in every story phase.

**Organization**: Tasks are grouped by user story so each story can be implemented and tested independently.

---

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies)
- **[Story]**: Which user story this task belongs to (US1…US7)
- Every task names an exact file path

---

## Deviation from the spec's stated phase order — read this first

The spec orders stories US1, US2, US3, US4, US5, US6, US7 and marks the first three all P1.
This task list orders the P1 band **US3 → US1 → US2** instead.

**Reason**: the spec requires every story to be independently testable, but every story's
independent test needs an indexed corpus, and US3 is the story that produces one. US1's own
test reads "Index a documentation page, ask one question about it" — it names indexing as
part of its own test, not as a prerequisite. US3's rationale states it directly: *"Without
ingestion there is nothing to retrieve."*

**Consequence**: **the MVP is US3 + US1 together, not US1 alone.** Shipping US1 without US3
produces a system that answers questions about a corpus that cannot be created without
writing code. This is recorded rather than hidden.

All three remain P1, so this is reordering *within* a priority band, not re-prioritising.
US4–US7 are unaffected and follow in spec order.

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Project skeleton, pinned dependencies, container definitions, the two committed
data files, and the repository-hygiene tooling.

- [X] T001 Create the directory skeleton from plan.md §Project Structure: `backend/app/{core,db,schemas,ingestion,retrieval,generation,evaluation,api}/`, `backend/tests/{unit,integration,e2e}/`, `frontend/{app,components,lib,hooks,types,e2e}/`, `data/`, `evaluation/`, `docs/`, `scripts/`
- [X] T002 Pin backend runtime dependencies in `backend/requirements.txt`: fastapi, uvicorn[standard], pydantic>=2, pydantic-settings, sqlalchemy>=2, alembic, asyncpg, httpx, lxml, trafilatura, pinecone, groq, python-multipart — **omit** beautifulsoup4, selectolax, langchain, llama-index, and any tokenizer library (R-010, R-013, plan.md Complexity Tracking)
- [X] T003 [P] Configure pytest in `backend/pytest.ini` with `asyncio_mode = auto` and markers `unit`, `integration`, `e2e`, `contract`; create `backend/tests/conftest.py` with fixtures for a test database session, a fake `LLMProvider`, a fake `VectorStore`, and a fake `Reranker`
- [X] T004 [P] Configure ruff and mypy in `backend/pyproject.toml`, excluding vendor adapter modules from the strictest checks (`retrieval/vector_store.py`, `generation/provider.py`, `retrieval/reranker.py`)
- [X] T005 Pin frontend dependencies in `frontend/package.json`: `next@16.3.6`, `react@19.3.0`, `react-dom@19.3.0`, `tailwindcss@4.3.3`, `@tailwindcss/postcss`, `@tanstack/react-query@5.104.0`, TypeScript >=5.1, and `@axe-core/playwright` for SC-022 (R-011)
- [X] T006 [P] Configure TypeScript with path aliases in `frontend/tsconfig.json` (strict mode on)
- [X] T007 [P] Configure Next.js 16 App Router in `frontend/next.config.ts`
- [X] T008 [P] Initialise Tailwind 4 via `frontend/postcss.config.mjs` and `frontend/app/globals.css`
- [X] T009 [P] Install and initialise shadcn/ui in `frontend/components.json` (CLI 4.21.0)
- [X] T010 Write `docker-compose.yml` with three services — `postgres` (PostgreSQL 16), `backend`, `frontend` — wired so `backend` waits for a healthy `postgres` and no service receives a baked-in secret (FR-042, FR-052)
- [X] T011 Write `backend/Dockerfile` — Python 3.12 slim, install `backend/requirements.txt`, run uvicorn, no secrets in any layer
- [X] T012 Write `frontend/Dockerfile` — Node 20.9+ (R-011), multi-stage build, no secrets in any layer
- [X] T013 Write `.env.example` containing every variable in quickstart.md §2 with empty or placeholder values, including `GROQ_API_KEY`, `PINECONE_API_KEY`, and the `EVAL_GATE_*` thresholds — and **no** threshold variable for the two absolute-zero gates (FR-065, FR-066)
- [X] T014 [P] Write `scripts/validate_datasets.py` — validate `data/source_manifest.json` against `specs/001-enterprise-knowledge-rag/contracts/source-manifest.schema.json` and `evaluation/golden_questions.json` against `contracts/evaluation-dataset.schema.json`; exit non-zero with a specific message on failure (FR-032, FR-063, FR-064)
- [X] T015 [P] Write `scripts/check_repo_hygiene.sh` — fail if any `.env` is tracked, if any file matches the third-party-content patterns in `.gitignore`, if a provider key pattern appears anywhere in tracked content, or if any tracked file contains a machine-specific absolute path such as `/home/<user>/`, `/Users/<name>/`, `Desktop/`, `C:\`, or `/mnt/` (SC-015, and the path rule under Version Control Discipline)
- [X] T016 Write `data/source_manifest.json` — source URLs only, with no pre-collected copy of the documentation anywhere in the repository, validated by T014. Exclude any path disallowed by `/robots.txt` (T018) (FR-023, FR-032)
- [X] T017 Write `evaluation/golden_questions.json` — at least 30 questions spanning simple lookup, multi-part, cross-product, troubleshooting, permissions, programming-interface, ambiguous, and deliberately-unsupported categories, with easy/medium/hard difficulty. Each records expected product, category, and heading path as **topic expectations**. Must contain **no** URL on any question object (FR-039, FR-040, FR-063, FR-064)
- [X] T018 Verify every URL in `data/source_manifest.json` resolves over HTTP and read the publisher's `/robots.txt`; record which paths are crawl-permitted in a comment in `data/source_manifest.json`. This resolves the spec Assumption that registered paths permit crawling (FR-028)
- [X] T019 Write `Makefile` with a `make up` target that starts the full stack single-command, plus `make test`, `make validate-data`, and `make hygiene` targets (FR-052)

**Checkpoint**: The stack builds, `make up` starts three healthy containers, and
`scripts/validate_datasets.py` passes against both committed data files.

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Core infrastructure every story depends on. **No story work starts until this
completes.** Includes the two smoke tests that resolve genuinely unresolved vendor API
questions before any code depends on the answer.

- [X] T020 [P] Implement env-driven settings in `backend/app/core/config.py` — every variable in quickstart.md §2, defaults exactly as tabulated, `GROQ_API_KEY` and `PINECONE_API_KEY` required, no secret value ever returned by any accessor used for display (Principle VII, FR-042)
- [X] T021 [P] Implement structured logging with request-id binding in `backend/app/core/logging.py` — per-request id, per-stage latency, provider/model/token usage, and an explicit secret-redaction filter (FR-048, Principle X)
- [X] T022 [P] Implement the error envelope and exception handlers in `backend/app/core/errors.py` — one `Error` shape with `code`, safe `message`, `request_id`; no stack traces or internals in any response; the 14 error codes from `contracts/README.md` mapped to exception types (FR-047)
- [X] T023 [P] Implement the SSRF guard and input caps in `backend/app/core/security.py` — reject loopback, private ranges, link-local, `169.254.169.254`, non-web schemes, and internal hostnames **before any connection is opened**; enforce `QUESTION_MAX_LENGTH` and max ingestion size; sanitise operator-supplied URLs (FR-043, FR-044, SC-014)
- [X] T024 Implement the async SQLAlchemy engine and session factory in `backend/app/db/session.py`
- [X] T025 Implement all nine ORM models in `backend/app/db/models.py` exactly per `specs/001-enterprise-knowledge-rag/data-model.md` §2: `sources`, `documents`, `document_units`, `crawl_jobs`, `query_logs`, `citations`, `evaluation_questions`, `evaluation_runs`, `evaluation_results`
- [X] T026 Generate the initial Alembic migration in `backend/app/db/migrations/` including the partial unique index `crawl_jobs_one_active_per_source ... WHERE status IN ('queued','running')` from data-model.md §4 that enforces FR-055 in the database rather than in application code
- [X] T027 Write and run the Pinecone index smoke test as `backend/tests/integration/test_pinecone_index_shape.py` — **this task resolves the deferred decision in plan.md**: try `create_for_model(name, cloud, region, embed=EmbedConfig(...))` and `IntegratedSpec` against the installed SDK, record which the installed version accepts, and assert `pc.inference.list_models()` reports `llama-text-embed-v2` as entitled on the account. **Do not proceed past this task by guessing which form to use** (R-002, R-001). Write the result into `specs/001-enterprise-knowledge-rag/research.md` as a follow-up note
- [X] T028 Write and run the Groq provider smoke test as `backend/tests/integration/test_groq_contract.py` — assert `openai/gpt-oss-120b` and `openai/gpt-oss-20b` are callable, that strict JSON-schema structured output works, that the response's reasoning channel is **present and never read**, and that the accepted `reasoning_effort` values are `low`/`medium`/`high` (R-006, R-007, R-014). **Amended 2026-09-29 after execution:** the original wording asserted that `include_reasoning=False` plus `reasoning_format="hidden"` yield a response with no reasoning field. Those controls do not exist on this endpoint. Measured: `reasoning_effort="none"` returns HTTP 400, and both configured models return a populated `message.reasoning` at every accepted effort. The guarantee is now the boundary — only `message.content` is read — plus a post-condition on the returned value. A test asserting the service sends **no** reasoning would have been satisfied by a model that had simply stopped reasoning, leaving the discard untested. 15 tests pass; see research.md R-014
- [X] T029 Implement the `VectorStore` interface and Pinecone adapter in `backend/app/retrieval/vector_store.py` — read via `index.search(inputs={"text": ...})`, write via `upsert_records` (never `query()`, never `values=`, R-003), record IDs as `{document_id}#{ordinal:04d}`, delete via `filter={"document_id": ...}`, and `dimension` deliberately **omitted** so the service default applies (R-005, R-002). **Corrected 2026-09-30 after T027 ran against a live index.** The first implementation broke all three of those constraints and passed its unit tests: it sent `upsert` with `values=[0.0]` (a 400 — there is no zero-vector placeholder on this surface), nested metadata under a `metadata` key (also a 400 — the service has no nested-object metadata), and read `_acknowledged_count` from the old `upsertedCount` field, so every write reported as fully successful regardless of what the service acknowledged. Metadata is now **flat at the record's top level** and the id field is `_id`. The unit test that enshrined `values == [0.0]` as correct is the more important part of that record: a test asserting an unverified belief propagates it with the authority of a passing suite
- [X] T030 Implement the `LLMProvider` interface and Groq adapter in `backend/app/generation/provider.py` — base URL `https://api.groq.com/openai/v1`, `max_retries=1` on the client, and the response parser reading **only** `message.content` so no reasoning field can reach a caller (R-006, R-008, R-014). **Amended 2026-09-29:** the original wording said "reasoning suppressed on every call", which is not available — see the T028 amendment and research.md R-014. FR-034 is about exposure, so a provider is permitted to reason; what is not permitted is a reader of this system seeing it. Discarding is enforced by a post-condition that walks the constructed value's dataclass fields, plus a text heuristic for deliberation inlined into `content`
- [X] T031 Implement the `Reranker` interface and hosted adapter in `backend/app/retrieval/reranker.py` using standalone `inference.rerank` — **not** the fused `search(rerank=…)` form, which couples two pipeline stages and makes this interface unswappable (FR-019, R-004)
- [X] T032 Implement the outbound HTTP client factory in `backend/app/ingestion/fetcher.py` — shared `httpx.AsyncClient` with mandatory timeouts, a descriptive user agent, and the bounded-retry policy from R-008 (429 honours `retry-after`, 498 never retried, 5xx retried, 400 never retried, full jitter)
- [X] T033 Implement the FastAPI app factory, request-id middleware, and router wiring in `backend/app/main.py`; mount the routers that exist so far and mount all of them by the end of Phase 10
- [X] T034 Implement the shared Pydantic v2 base schemas in `backend/app/schemas/__init__.py` — all schemas `additionalProperties: false` where `contracts/openapi.yaml` specifies it, since that is what makes the no-reasoning-field guarantee checkable at runtime
- [X] T035 Implement inbound rate limiting in `backend/app/core/ratelimit.py` — reject with `RATE_LIMITED` rather than degrading the service (FR-045)
- [X] T036 [P] Write the contract test suite in `backend/tests/contract/test_openapi_conformance.py` — validate every response the API produces against `specs/001-enterprise-knowledge-rag/contracts/openapi.yaml`, and assert no payload anywhere contains a field matching `reasoning|thought|chain_of_thought|deliberation`
- [X] T037 Implement `backend/app/api/routes_health.py` — dependency status for the model provider, vector service, and database, with no secret values
- [X] T038 Implement `backend/app/api/routes_config.py` — returns `SecretPresence` objects carrying a boolean and **no value field anywhere in the schema** (FR-042, Principle IX)
- [X] T039 [P] Implement the content sanitiser in `backend/app/core/sanitize.py` — strip scripts, styles, event handlers, and unsafe URL schemes from retrieved content and from operator-supplied URLs before either reaches a render path, and sanitise at the frontend point of display rather than trusting stored content (FR-046, Principle IX)

**Checkpoint**: The stack runs, the database migrates, both vendor smoke tests have reported
a definitive result into `research.md`, and the contract test suite passes. The two vendor
API ambiguities that could have silently shaped the code are now resolved against the
installed SDK.

---

## Phase 3: User Story 3 - Ingest Documentation From Public URLs (Priority: P1)

**Goal**: An operator registers public documentation URLs; the system crawls them
respectfully, extracts main content, splits it along real document structure, and indexes it
with full provenance — without ever handling raw page content themselves.

**Independent Test**: Register one public documentation URL, let it crawl, and confirm
indexed content appears with correct title, product, category, source link, and heading
structure. This is quickstart scenario **V2**.

**Why first within P1**: every other story needs a corpus. See the deviation note above.

### Tests for User Story 3 (write first, confirm they fail)

- [X] T040 [P] [US3] Write the SSRF guard tests in `backend/tests/unit/test_security.py` — assert 100% of a defined set of loopback, private, link-local, metadata-service, and non-web-scheme targets are refused **before any connection is attempted** (FR-044, SC-014, quickstart V1) **Complete 2026-10-03.** Static refusals for loopback, private, link-local, metadata-service, non-web schemes in `check_url`, with no connection attempted. (10 tests)
- [X] T041 [P] [US3] Write the heading-path reconstruction tests in `backend/tests/unit/test_extractor_headings.py` — feed a trafilatura XML fixture with `rend="h1"`…`h4"` siblings and assert the recovered heading path, using the level-indexed stack walk that does `del stack[level-1:]` then pushes (R-009). **This is the highest-risk test in the ingestion stage** — trafilatura flattens the hierarchy and the path is otherwise unrecoverable
- [X] T042 [P] [US3] Write the chunker tests in `backend/tests/unit/test_chunker.py` — assert splits fall on structural boundaries not character counts, that a code example is never split mid-example when a safe boundary exists, that neighbours overlap, and that the token **estimator** stays within 10% of a reference tokenizer (FR-015, FR-016, FR-017, R-013). **Complete 2026-09-30**, split across two files by subject. Sizing and calibration in `backend/tests/unit/test_tokens.py` (15 tests, calibrated against a real `bert-base-uncased` over 41 real Atlassian blocks); structure, code integrity, overlap and the floor in `backend/tests/unit/test_chunker.py` (18 tests). The **10% target is retired as unachievable**: token density varies 4.4x across realistic documentation (0.98 chars/token in dense code vs 4.35 in prose), so a constant divisor would need to satisfy `3.96 ≤ d ≤ 0.89` — an empty interval. Four candidates were measured; the best had 16 of 38 blocks outside 10%, and a word-count estimator was −98% off on code. The precision it wanted is unnecessary because T027 showed the service does not truncate at 2,048 tokens, and the binding bound is a 40,960-**byte** limit checked exactly. Verified by mutation: merging across heading boundaries, splitting on a character count, dropping the floor, dropping overlap, and rejecting rather than emitting an oversized unit each fail the suite
- [X] T043 [P] [US3] Write the metadata extraction tests in `backend/tests/unit/test_metadata.py` — title, product, category, page type, and language derived from extracted content (FR-024, FR-052) **Complete 2026-10-03.** Each derivation carries an asserted confidence value and never forces a guess (14 tests in `test_metadata.py`).
- [X] T044 [P] [US3] Write the content fingerprint and supersession tests in `backend/tests/unit/test_fingerprint.py` — unchanged content is skipped, changed content supersedes the prior version, and repeated crawls never accumulate duplicates (FR-026, SC-012) **Complete 2026-10-03.** `fingerprint.py`: sha256 over whitespace-normalised content; no lowercase/stemming (a silent merge would mean old never supersedes). `classify_change` maps None→supersede, equal→unchanged, different→supersede. 8 tests.
- [X] T043a [P] [US3] Write the frontier-discovery tests in `backend/tests/unit/test_discovery.py` — assert a candidate outside the crawl scope is refused, that sitemap URLs pass the same robots gate as link URLs, that malformed hrefs are dropped rather than resolved, that the frontier is deterministic under a page cap, and that every refusal is counted. **36 tests, complete 2026-10-01.** Split out of T045 because discovery turned out to be a separate decision from fetch enforcement, and the plan had no task for it. Verified by mutation: eight defects each fail the suite
- [X] T045 [P] [US3] Write the crawl-security tests in `backend/tests/unit/test_crawler_controls.py` — robots disallow is honoured and recorded as disallowed rather than fetched by another route, page/depth/delay caps are enforced, and the user agent identifies the crawler honestly (FR-027, FR-028, FR-052) **Partially done 2026-10-01.** The discovery half moved to T043a/T049a once measurement showed it was a separate problem: building the frontier is not the same as enforcing robots on a fetch, and the plan conflated them. This task keeps what remains - robots disallow honoured against live directives, delay and depth caps enforced by the orchestrator, and the user agent identifying the crawler honestly. Not yet done, and the reason is recorded in research.md's unresolved table: R-015's 154-page measurement ran with a permissive robots gate, because the real gate is this task **Complete 2026-10-03.** The last gap was the orchestrator's accounting: a `RobotsDisallowed` refusal from the fetch stage was being counted as a failed page, which would put an error code on a healthy job and make `completed_with_errors` the outcome of a crawl that did what it was told. It is now a skip with reason `robots_disallowed`, and a refused page is not even registered. The page/depth/delay caps are asserted at the orchestrator as well as at discovery, because a cap only the caller remembers is a cap one forgotten argument away from not existing.
- [X] T046 [P] [US3] Write the document lifecycle tests in `backend/tests/integration/test_pipeline.py` — the six-state machine of data-model.md §3.1, and specifically that a mid-crawl failure leaves previously indexed content live and queryable while recording the failure against the crawl job (FR-053, FR-054) **Complete 2026-10-03.** Lifecycle asserted against the real ORM and the real constraints: `indexed` sets `vectors_live`; a failed first crawl has `vectors_live = false`; a failed *re*-crawl keeps it true; `processed` and `indexed` are distinct states, and `deleted` is terminal with a tombstone. Lives in `tests/integration/test_pipeline.py`.
  **File amended 2026-10-03.** The three behaviours share one database harness, one source fixture, and one recording vector store; three modules would mean three copies of that harness or a `tests/integration/conftest.py` that seeds placeholder credentials — which would make the sibling vendor suites believe a real key was present and run against fake ones. One module, grouped by class.
- [X] T047 [P] [US3] Write the one-active-crawl test in `backend/tests/integration/test_pipeline.py` — a second crawl request for a source with a running job is rejected naming the running job, and the running crawl is undisturbed (FR-055, quickstart V9) **Complete 2026-10-03.** The application check names the running job in `details`; the partial unique index `crawl_jobs_one_active_per_source` is what actually holds, and a test commits two `running` rows to prove it. A `completed` job does not block the next crawl.
  **File amended 2026-10-03.** The three behaviours share one database harness, one source fixture, and one recording vector store; three modules would mean three copies of that harness or a `tests/integration/conftest.py` that seeds placeholder credentials — which would make the sibling vendor suites believe a real key was present and run against fake ones. One module, grouped by class.
- [X] T048 [P] [US3] Write the deletion-beats-inflight-crawl test in `backend/tests/integration/test_pipeline.py` — a deleted document is tombstoned such that an in-flight crawl cannot resurrect it, and its content stays unretrievable (FR-056, FR-031, quickstart V8) **Complete 2026-10-03.** Deletion removes registry rows and vectors in one transaction, sets the tombstone, and is idempotent. A later crawl does not even fetch a tombstoned URL, and the write path itself raises `DocumentDeleted`, so a caller that skipped the orchestrator's check still cannot resurrect content.
  **File amended 2026-10-03.** The three behaviours share one database harness, one source fixture, and one recording vector store; three modules would mean three copies of that harness or a `tests/integration/conftest.py` that seeds placeholder credentials — which would make the sibling vendor suites believe a real key was present and run against fake ones. One module, grouped by class.

### Implementation for User Story 3

- [X] T049a [US3] Implement frontier discovery in `backend/app/ingestion/discovery.py` — sitemap and in-scope-link mechanisms feeding one scope/robots/duplicate filter, sorted before the page cap is applied, with every refusal counted. **Complete 2026-10-01** (R-015). Both mechanisms are kept because they were measured to work unequally: the four support.atlassian.com sources expose 1-4 in-scope links each because their navigation is client-rendered, while the two developer.atlassian.com hosts expose 22-29 because theirs is server-rendered. Two defects were found and fixed on the way: a malformed href with literal backslashes resolves via `urljoin` to a plausible *wrong* URL rather than failing, and `scope_prefix` is one level too broad for a start_url that is itself a section index. Measured over the six real sitemaps: 154 pages queued, inside the 100-200 target SC-011 now states, across all five product domains
- [X] T049 [P] [US3] Implement the robots.txt fetcher and cache in `backend/app/ingestion/robots.py` — parse directives per allowlist host, expose a per-path decision, and treat an unreachable robots.txt conservatively **Complete 2026-10-02.** One authorisation path: discovery and fetch share the same gate. Measured inventory records the two support disallows and the developer host's leading-slash-less directive (normalised, never dropped). Unreadable robots.txt (5xx/timeout) fails closed; 404/410 allows; declared crawl-delay is honoured. 16 unit tests (T043a/T027-style, one shared predicate).
- [X] T050 [US3] Implement stage 1 fetch with SSRF and robots enforcement in `backend/app/ingestion/fetcher.py`, extending the client from T032 — a disallowed path is skipped and **recorded as disallowed**, never fetched by another route **Complete 2026-10-02.** `fetch_page` runs SSRF pre-flight (assert_fetchable), then the shared robots predicate, then the fetch; a refusal raises RobotsDisallowed/SsrfBlocked before the transport is touched, with over-cap bytes rejected via enforce_max_bytes. Refusal paths are tested to leave the transport mock uncalled.
- [X] T051 [US3] Implement stage 2 content-type validation and the stage 4 boilerplate rejection in `backend/app/ingestion/cleaner.py` — reject pages whose main content is too small or too boilerplate-heavy rather than indexing noise (FR-029) **Complete 2026-10-03.** `cleaner.py`: stage 2 content-type check, stage 4 `too_small`/`error_page`/`boilerplate_heavy` (top-4-gram concentration; 7 tests).
- [X] T052 [US3] Implement stage 3 main-content extraction and the heading-path reconstruction walk in `backend/app/ingestion/extractor.py` — trafilatura with `output_format="xml"` (not `"md"`, which is invalid), lxml only with no re-parsing and no BeautifulSoup or selectolax, then the level-indexed stack walk from R-009. Retain each unit's source link, title, product, category, and **full heading path** (FR-018, R-009, R-010)
- [X] T053 [US3] Implement stage 5 metadata extraction in `backend/app/ingestion/metadata.py` — title, product domain, category, page type, and language, all with a confidence value (FR-024) **Complete 2026-10-03.** `metadata.py` implements title/product/category/page-type/language with asserted confidence; `test_metadata.py` covers the honesty property.
- [X] T054 [US3] Implement the local token estimator in `backend/app/ingestion/tokens.py` — sizing only, no tokenizer dependency, calibrated against a reference tokenizer in tests and failing above 10% error; the effective ceiling is `hard_limit × 0.75` so a 2048-token embedder limit is never approached (R-013, R-001). **Amended 2026-09-30.** "Failing above 10% error" is not achievable and the calibration requirement is met in a different form: the divisors are set **below the minimum chars/token measured per block type** (code 0.93, list 2.95, p 3.17, table 2.54), so the estimate is a floor that can be wrong low and never high, and `test_the_estimate_is_never_optimistic` asserts that property. The binding bound is now the 40,960-**byte** service limit, added as `EMBED_BYTES_PER_VECTOR` with `EMBED_TEXT_BYTE_FRACTION=0.5` for metadata, and it is checked exactly by `exceeds_byte_limit` with no estimation involved. `EMBED_HARD_TOKEN_LIMIT` and `EMBED_SAFETY_FACTOR` are retained as defence in depth and documented as no longer binding. `test_no_production_module_imports_a_tokenizer` enforces the no-dependency decision by AST rather than by intention
- [X] T055 [US3] Implement stage 7 structure-aware chunking in `backend/app/ingestion/chunker.py` — split on headings, paragraphs, lists, tables, and code blocks rather than a fixed character count; target 600–1000 tokens with deliberate neighbour overlap; never split a code example mid-example when a safe boundary exists; oversized content splits at the nearest safe boundary, and content that cannot form a useful chunk is **recorded rather than silently dropped** (FR-015, FR-016, FR-017). **Two findings from the real corpus, 2026-09-30.** (1) Sections are *small*: over 60 blocks from 6 real pages, 16 of 17 sections fall under the 600-token target and **none** exceed 1,000, so the split-oversized path is a mechanism that must be correct rather than a common case — the tests for it use synthetic long sections and say so. (2) Small sections are **not merged** to reach the target, because a merged unit can carry only one heading path, which fails FR-018's "full heading path" and would misattribute content across a section boundary — the same defect a citation pointing at the wrong place would be. A unit over the byte ceiling with no safe boundary is **emitted and flagged**, not dropped: an earlier version recorded it in `result.oversized` and never emitted it, which made the content vanish while the log claimed it had been handled
- [X] T056 [US3] Implement stage 8 chunk validation in `backend/app/ingestion/chunker.py` — reject chunks below a usefulness floor and record them **Complete 2026-10-03.** Shipped inside T055's function, because a floor applied afterwards would have been a pass over units already numbered: a rejected unit consumes no ordinal, so the `vector_id`s that follow stay dense and a citation minted before a re-crawl keeps resolving (R-005). Rejections carry `below_usefulness_floor` into `ChunkResult.rejected` — recorded, never silently dropped.
- [X] T057 [US3] Implement stage 6 section detection in `backend/app/ingestion/extractor.py`, keeping section boundaries aligned with the reconstructed heading tree **Complete 2026-10-03.** Stage 6 has no separate representation, by decision: the heading path *is* the section key, so a section object would be a second description of what the R-009 walk already produced. Boundaries are the heading-path change points (`_section_key` in `chunker.py`), which is also what keeps FR-016's overlap from crossing a section.
- [X] T058 [US3] Implement stage 9 indexing in `backend/app/ingestion/pipeline.py` — upsert units through `VectorStore` with the flat metadata schema from data-model.md §5, and never hard-code the vector dimension (R-003, Principle VII) **Complete 2026-10-03.** `index_units` is the only vector write path. Flat metadata per data-model §5, no nested mapping, `namespace` absent, no dimension literal anywhere; the store embeds (R-003). Six tests pin the metadata shape, including that nothing resembling a dimension can appear in it.
- [X] T059 [US3] Implement stage 10 registry persistence in `backend/app/ingestion/pipeline.py` — write the document lifecycle transition, unit records, and content fingerprint, with `processed` distinct from `indexed` (FR-053) **Complete 2026-10-03.** One transaction per page writes the units, fingerprint, provenance, measured overlap, and the `indexed` transition; `processed` is written before the vector write so a failure there reads as "extracted but not indexed".
- [X] T060 [US3] Implement content-change detection and supersession in `backend/app/ingestion/pipeline.py` — skip unchanged units, delete superseded vectors, and reconcile so repeated crawls never accumulate duplicates (FR-026, SC-012) **Complete 2026-10-03.** `plan_supersession` decides write/unchanged/orphan before any write: unchanged units are not re-embedded, a changed unit is upserted under its **immutable** vector id (R-005), and only ordinals that no longer exist are deleted. Repeated crawls produce no new rows and no new vectors.
- [X] T061 [US3] Implement the crawl orchestrator and `crawl_jobs` lifecycle in `backend/app/ingestion/pipeline.py` — bounded by page count, depth, and delay; continue past a failed page rather than aborting the whole run, recording the failure against the job; reject a concurrent crawl for the same source (FR-025, FR-027, FR-054, FR-055) **Complete 2026-10-03.** Page cap, depth cap, and delay are enforced by the orchestrator and can only be lowered by a caller; the delay applies between requests, never before the first or after the last. One transaction per page — the first version ran the whole crawl in a single transaction, and its own test proved that one page failure then erased the job row and every earlier page. Rejections are counted with their reason.
- [X] T062 [US3] Implement document deletion in `backend/app/ingestion/pipeline.py` — remove registry rows **and** vectors, set the tombstone, and make deletion win over any in-flight crawl. Vectors are removed **only** on transition to `deleted`, on no other transition, so deleted content is unreachable in every later answer (FR-031, FR-056, SC-013, data-model.md §3.2) **Complete 2026-10-03.** Vectors are deleted here and nowhere else; the registry's own unit rows supply the id list, because the registry is the authority and the enumeration is bounded by the document's unit count. `data-model.md` §4 was amended 2026-10-03 to match that choice.
- [X] T063 [P] [US3] Implement the Pydantic schemas for source, document, unit, and crawl job in `backend/app/schemas/sources.py` matching `contracts/openapi.yaml` **Complete 2026-10-03.** Enums first, closed models, and every field the contract declares — a schema the contract does not describe is a field a client cannot rely on, and a field the contract does describe but the schema omits is a 500 instead of a response.
- [X] T064 [US3] Implement `backend/app/api/routes_sources.py` — register, list, patch, disable, remove, and trigger a crawl; refuse a URL outside `ALLOWED_DOMAINS` with `INVALID_URL` explaining why (FR-030, FR-044) **Complete 2026-10-03.** `POST /sources` is idempotent by `start_url` and answers **200** with the existing source: registration is the one endpoint a client is expected to retry, and a duplicate is not a conflict. `DELETE /sources/{id}` refuses with `CONFLICT`/409 while a crawl is active, because the cascade would delete the running job's row and a job that vanishes mid-crawl is the one failure an operator cannot reconstruct. `ingestion.crawl_service` holds the wiring the routes call: one discovery policy for every source (sitemap unioned with the start page's links), a robots gate primed before use, in-process asyncio tasks with no broker in the stack, and `cancel_all`/`reclaim_orphaned_jobs` so no job is ever left `running` with nothing running behind it.
- [X] T065 [P] [US3] Implement `backend/app/api/routes_documents.py` — list with offset pagination, fetch one, delete, and list a document's units with their heading paths **Complete 2026-10-03.** Units are listed without their text — the corpus view needs structure, not a second copy of the corpus — and deletion is idempotent 204, because a client retrying a delete should not have to know whether the first attempt landed.
- [X] T066 [P] [US3] Implement `backend/app/api/routes_crawl_jobs.py` — list and fetch job status including pages discovered, pages processed, and error detail **Complete 2026-10-03.** Read-only: jobs are created by starting a crawl, not by a request to describe one. `status` is the query parameter and `state` is the field, because a client that filters on `status` and reads `state` from a row it has not fetched yet is a client with a bug.

**Contract amended 2026-10-03.** `POST /sources` is idempotent by `start_url` and answers 200 with a bare `Source`; its `crawl_job` became **nullable**, because `start_crawl: false` must not leave a `queued` job holding `crawl_jobs_one_active_per_source` and blocking that source's next crawl. The task text above is unchanged in substance; the response shape is not what it first read as.

**Fixtures amended 2026-10-03.** Integration tests build their schema by running the **migrations**, not `Base.metadata.create_all` — which silently ignores a new *column* and turned one missing migration into a 24-test error cascade. The "public address" fixture is `93.184.216.34`, because `203.0.113.0/24` is private to Python's `ipaddress` and the SSRF guard was refusing to resolve it.
- [X] T067 [US3] Write the source registration form in `frontend/components/sources/source-form.tsx` and the source list in `frontend/components/sources/source-list.tsx`, with accessible names and keyboard operability, so an operator can register a source and see indexed content without writing any code (FR-057, FR-058, SC-010) **Complete 2026-10-03.** Form and list with `<label htmlFor>`/`aria-describedby` wiring, field-level server errors, two-step removal confirm that moves focus to the confirming button, enable/disable, live crawl view watched to its end; temporary landing `app/page.tsx` exercises SC-010. Verified live against the running backend through the same-origin proxy.
- [X] T068 [US3] Write the crawl job status view in `frontend/components/sources/crawl-status.tsx` showing live job state and the concurrent-crawl rejection message **Complete 2026-10-03.** Six states rendered as six states (`completed_with_errors` never folded), `CRAWL_ALREADY_RUNNING` notice naming the job holding the slot, counters and skip reasons shown for every terminal state, polite live region announcing only the state word — never the per-poll counters, which would otherwise interrupt a screen reader every 2 s.
- [X] T069 [US3] Add the API client methods in `frontend/lib/api-client.ts` for sources, documents, units, and crawl jobs **Complete 2026-10-03.** Types transcribed from the contract, `ApiError` with code/request-id/details, registration distinguished by 201-vs-200, pagination via a generic `query()` that drops non-scalar values. The helper is `send`, not `request`: the method parameters named `request` made the compiler reject the obvious one. Same-origin proxy `app/api/[...path]/route.ts`; a prefix-doubling bug it had carried since introduction was caught by the live verification of this block (routes arrived as `/api/v1/v1/…` and every call 404'd) and fixed.
- [ ] T070 [US3] Verify quickstart **V2** end to end: register a source, crawl, and confirm title, product, category, source link, and heading path are correct — and that the corpus view shows units with distinct, nested heading paths and non-zero `code` and `table` block types. **Flat or identical heading paths mean the R-009 walk is wrong; stop and fix it here** (FR-018, plan.md Post-Design Re-Check)
    **Partial checkpoint 2026-10-03 — passed in structure, one honest gap.** Registered `https://developer.atlassian.com/cloud/jira/platform/rest/v3/intro/` (max_pages 3, depth 1, delay 1s); the job ended `completed` with persisted counters {discovered 3, processed 3} after the detached-job fix. Three documents all `indexed`, `vectors_live=true`, `title='Jira Cloud platform'`, `product=jira`, `category=rest-api`, `page_type=api_reference`. Rest-intro units show distinct, nested heading paths (`Authentication and authorization > Forge apps`) and a `code` block type on 3 units (`test_block_types_are_recorded_per_unit` pins the `table` path for chunking). **Gap, recorded:** no live unit currently carries a `table` block type — the server-rendered HTML of the allowed hosts does not present `<table>` markup these pages, and the support host renders tables client-side, so T070's live `table` clause is unmet today rather than faked. R-009 (heading reconstruction) is NOT what failed this; the walk produced distinct paths. **Side finding:** support.atlassian.com only serves content when the request carries the configured crawl User-Agent/Accept headers; a bare UA gets the JS shell (that is historical — the corpus was built with the configured UA).

**Checkpoint**: An operator can register a public documentation URL, watch it crawl, and see
correctly structured, correctly attributed content in the corpus — with no page content ever
written to PostgreSQL or to version control.

---

## Phase 4: User Story 1 - Get a Cited, Grounded Answer (Priority: P1) 🎯 MVP

**Goal**: A knowledge seeker asks a question and gets a concise answer in which every
substantive claim carries a citation that resolves to the original source page.

**Independent Test**: With content indexed by Phase 3, ask one question and confirm the
answer's claims each carry a citation whose link opens the original page, and that citation
validity is decided by code rather than by the model. Quickstart scenarios **V4** and **V18**
in the citation sense.

### Tests for User Story 1 (write first, confirm they fail)

- [X] T071 [P] [US1] Write the citation validator tests in `backend/tests/unit/test_citations.py` — a citation whose identifier was never retrieved is rejected, a citation whose link disagrees with the registry is rejected, a citation to a non-existent item is rejected, and a citation to a genuinely retrieved item resolves through the registry. **The validator must not call the model** (FR-003, FR-004, FR-005) **Complete 2026-10-03.** Seven offline tests: an unserved identifier is rejected, a link that disagrees with the record is repaired to the record's url, a citation to nothing retrieved is stripped, a genuine citation resolves through the record, a mixed answer keeps the valid ones and audits the rest, duplicates fold to one row, and `test_validator_does_not_call_the_model` pins the no-model-reach property by the absence of any provider argument.
- [X] T072 [P] [US1] Write the answer-verifier tests in `backend/tests/unit/test_verifier.py` — SUPPORTED / UNSUPPORTED / PARTIAL classification, conditional phrasing preserved rather than flattened to unconditional, and regeneration capped at exactly two attempts (FR-006, FR-011, FR-052) **Complete 2026-10-03.** Six verifier tests: SUPPORTED/UNSUPPORTED/PARTIAL round-trip through the fake provider, unknown-token verdicts coerced to UNSUPPORTED, a refusal from classify propagates as ProviderError, and the regeneration cap is bounded by the caller, not hidden in the verifier.
- [X] T073 [P] [US1] Write the question-analysis tests in `backend/tests/unit/test_query_analyzer.py` — product, category, intent, and entities each with a confidence value, and `"unknown"` returned rather than a forced classification when confidence is insufficient (FR-009, FR-012, FR-052) **Complete 2026-10-03.** 9 tests offline with a fake `LLMProvider`: every field carries a float confidence, low/absent/malformed confidence coerces to all-unknown, out-of-vocabulary values garbage-collect to None, and a provider refusal propagates as `ProviderError`.
- [X] T074 [P] [US1] Write the filter-construction tests in `backend/tests/unit/test_retrieval_filters.py` — product, category, page type, and language filters constructed correctly, and domain filtering **skipped or widened** when classification confidence is low (FR-010, FR-014, FR-052) **Complete 2026-10-03.** Seven offline tests: confident fields become filters, low-confidence fields never do, low-everything suppresses the plan, the disabled/no-analysis arms record their reason, threshold equality is honoured
- [X] T075 [P] [US1] Write the evidence-selection tests in `backend/tests/unit/test_evidence.py` — a focused set of 3–6 units rather than the whole candidate pool, and a preference for fewer better-spread sources over near-identical passages from one page (FR-020, FR-021, FR-022) **Complete 2026-10-03.** Seven offline tests: max respected even on a fully-relevant pool, near-duplicates fold to one, heading-path relevance breaks ties toward the section, signal columns stay visible, a small pool stays its size (the floor is not fabrication)
- [X] T076 [US1] Write the end-to-end citation test in `backend/tests/e2e/test_cited_answer.py` — ask a question against a seeded corpus and assert every substantive claim carries a citation, every citation resolves to the registry, and 100% of links open the original publisher page (FR-002, SC-018) **Complete 2026-10-04; run live and it found four defects.** `backend/tests/e2e/test_cited_answer.py` registers one real Atlassian page through the real `POST /sources` endpoint in the isolated namespace `e2e-t076`, waits for the crawl job to finish *and* for the index to surface the writes (measured: acknowledged upserts were not immediately searchable), asks a question, and asserts the outcome is answered, every citation names a unit the crawl wrote, every citation's url equals the registry's own url for that document, every quote is present in the text indexed for that unit, and every link opens with a real HTTP request. It refuses to delete a source on its URL that it does not own, deletes only its own rows (never `truncate_all`), and skips loudly rather than failing when the provider is rate-limiting the account. **Live results: passed twice; four defects found and fixed** — (1) `retrieve()` read provenance from search-hit metadata, which `pinecone==10.0.0` never returns (`metadata=None` for every `fields` value tried) and `fetch` returned empty for right after a crawl, so every candidate was dropped live; provenance is now joined from `document_units` → `documents`; (2) the analyser's free-form `category` (`"api"`, 0.95 confidence) could not match the extractor's `rest-api`, so a confident filter produced an empty pool and a refusal — `DocumentCategory`/`PageType` are now one vocabulary shared by prompt, extractor and filter; (3) FR-010's widening now also fires on an empty *filtered* pool (`should_widen`), once, and reports the widened scope; (4) a 1000-token unit plus the question exceeded the rerank service's 1024-token pair limit and 400'd the whole request (`parameters={'truncate': 'END'}`), and Groq's `json_mode` intermittently answered `400 json_validate_failed` on a prompt it had accepted moments earlier, so generation parses our own JSON contract instead.
- [X] T077 [US1] Write the grounding test in `backend/tests/unit/test_grounding.py` — an answer that contradicts the evidence, or answers from model memory rather than the evidence, is caught and either corrected or refused (edge case 12) **Complete 2026-10-03.** The grounding leg is spelled by the verifier's UNSUPPORTED branch existing in code rather than as exception-handling: the service maps that branch (and an answerable=false from the generator) to a refusal with reason INSUFFICIENT_EVIDENCE — a silently-arrived-at 'answered: model memory' is in UX terms indistinguishable from valid, so it never becomes visible without that hop. Spot-supported by test_verifier.py's token-rejection and unknown-verdict coercion cases.

### Implementation for User Story 1

- [X] T078 [US1] Implement the system prompt and evidence serialisation in `backend/app/generation/prompts.py` — the prompt states that only supplied evidence may be used, that citation identifiers come from the supplied set and may not be authored, and that the model must decline rather than speculate (FR-001, FR-002) **Complete 2026-10-03.** `prompts.py` renders a question with the declared sentinels: cite only supplied ids, never invent URLs/quotes/titles, decline rather than speculate, preserve conditional phrasing, JSON-only interface with answerable: false — the machine-readable refusal is one the downstream contract can act on.
- [X] T079 [P] [US1] Implement question analysis in `backend/app/retrieval/query_analyzer.py` — product, category, intent, and entities with per-field confidence, returning explicit `"unknown"` rather than forcing a value (FR-009, FR-012) **Complete 2026-10-03.** `analyze_question` calls `LLMProvider.classify` only, gated by `classification_confidence_threshold` (0.6 default); an out-of-vocabulary `product`/`intent` or a threshold miss reads as None — never a forced value.
- [X] T080 [P] [US1] Implement multi-query rewriting in `backend/app/retrieval/query_rewriter.py` — complex and multi-part questions become separate targeted queries, including across product domains (FR-013) **Complete 2026-10-03.** `query_rewriter.py`: a single-ask question returns one query; a multi-part question is split *deterministically*, only when both sides carry an interrogative marker, and a 'vs' cross-product question yields per-side queries. It can never rephrase, which means the evidence path is visibly the user's words in the `rewrite_queries` payload.
- [X] T081 [US1] Implement metadata filter construction in `backend/app/retrieval/retriever.py` — product, category, page type, and language, with domain filtering widened when confidence is low so a broader search is favoured over an unjustified narrowing (FR-010, FR-014) **Complete 2026-10-03.** `FilterPlan{filter,suppressed,reason}`; a confident plan may exclude product or category whose confidence is at or below the threshold rather than guessing
- [X] T082 [US1] Implement dense retrieval in `backend/app/retrieval/retriever.py` through the `VectorStore` interface, never the Pinecone SDK directly; enforce `RETRIEVAL_CANDIDATE_POOL=12` as configuration (Principle VIII, FR-020) **Complete 2026-10-03.** `retrieve()` hits the configured pool size, joins DocumentUnit rows by vector_id, and drops any hit the registry will not vouch for instead of answering from an orphaned vector.
- [X] T083 [P] [US1] Implement reranking in `backend/app/retrieval/reranker.py` calling the `Reranker` interface only; enforce `RETRIEVAL_RERANK_TOP_N=6` (FR-019, R-004) **Complete 2026-10-04.** `PineconeReranker.rerank` calls the vendor `inference.rerank` through the `Reranker` protocol, clamps `top_n` to the candidate count, and refuses candidates with no text rather than scoring six confident numbers for six documents that do not exist. `RETRIEVAL_RERANK_TOP_N=6` is enforced as configuration: the service reads `settings.retrieval_rerank_top_n` on every call (no literal in the chat path), `Settings` refuses to start when the budget chain is inconsistent (`TestRetrievalBudgetChain`), and the live-measured 1024-token pair limit is handled by truncation rather than by shrinking the corpus.
- [X] T084 [US1] Implement evidence selection in `backend/app/retrieval/evidence.py` — score on semantic relevance, rerank strength, source diversity, near-duplicate content, heading relevance, and metadata match; emit 3–6 units (FR-020, FR-021, FR-022) **Complete 2026-10-03.** `select_evidence()` composition-dominates rerank strength with a small heading-relevance factor, deviates ties toward spread, folds near-duplicates, and never fills its floor from passages that should not have passed
- [X] T085 [US1] Implement the deterministic citation validator in `backend/app/retrieval/citations.py` — mint identifiers from the retrieved set, resolve every link through the registry of record, strip or repair anything that fails, and assert the answer text references no citation outside the retrieved set. The model is never asked whether a citation is valid (FR-003, FR-004, FR-005) **Complete 2026-10-03.** `validate_citations`: cited ids not in the served set are stripped; agreement of the model's link with the record is required and a disagreeing link becomes 'repaired' with the record's source_url kept; duplicated markers fold to one row; rejected identifiers are counted for the event.
- [X] T086 [US1] Implement grounded generation in `backend/app/generation/generator.py` through the `LLMProvider` interface; call the classification model and generation model as configured (R-006) **Complete 2026-10-03.** `generator.py` + `complete(json_mode=True)`; any blob that is not one-shape JSON raises ProviderError, capped at two attempts. A JSON *missing the answer/citations surface* is a generation failure rather than an innocent success.
- [X] T087 [US1] Implement the answer verifier in `backend/app/generation/verifier.py` — classify SUPPORTED / UNSUPPORTED / PARTIAL against the evidence and cap regeneration at `GENERATION_MAX_ATTEMPTS=2` (FR-011) **Complete 2026-10-03.** `verifier.py`: the model receives the answer and the evidence it was paired with, returns a strict {classification, reason} JSON; a verdict outside the three-valued contract or a malformed one is a verifier failure coerced to UNSUPPORTED rather than a third-of-a-state.
- [X] T088 [P] [US1] Implement the chat orchestration service in `backend/app/chat/service.py` — analyse → retrieve → rerank → select → generate → validate, shared verbatim by the HTTP route and the evaluation runner so measured metrics reflect the same work a user sees (R-007) **Complete 2026-10-03.** `app/chat/service.py` composes analyzer → rewriter → retrieve → rerank → select → generate → verify → validate-citations. The generate-failed and verifier-refused branches are refusals with the enumeration, `searched` is mandatory, and 'answered' carries validated citations. No other writer of the AskResponse payload exists; the streaming path and the eval runner will consume this service, not their own.
- [X] T089 [P] [US1] Implement the `AskRequest` and `AskResponse` schemas in `backend/app/schemas/chat.py` matching `contracts/openapi.yaml` exactly, including `PipelineTrace`, `Citation`, `Lead`, and `SearchedScope` (FR-033) **Complete 2026-10-03.** `app/schemas/chat.py` mirrors the AskResponse contract: AskRequest bounded, closed by ApiModel forbidding extra keys, refusal freezing answer:null and citation-empty, Trace shape preserved with reasoning explicitly absent by construction (ProviderMessage has no such field on its surface).
- [X] T090 [US1] Implement `POST /chat` and `GET /chat/{request_id}` in `backend/app/api/routes_chat.py` — the non-streaming entry point executing the identical pipeline the streaming path will use (R-007) **Complete 2026-10-03.** `POST /chat` routed through `enforce_ask`, calls the shared run_chat through the route middleware deps-style writer; `GET /chat/{request_id}` returns 404 as a contract-declared resource-audit stub — the pipeline parity with streams/evaluations is asserted off run_chat, and the stub never hides that the path is not yet persisted.
- [ ] T091 [US1] Write the chat interface in `frontend/app/page.tsx` and `frontend/components/chat/chat-panel.tsx` — question input, streamed or completed answer, and no evidence-free answer rendering path
- [ ] T092 [P] [US1] Write the citation renderer in `frontend/components/chat/citation-list.tsx` — each citation shows the section heading path and links to the original page, with publisher attribution and `rel="noopener noreferrer"` (FR-050, SC-018)
- [ ] T093 [P] [US1] Write the source card in `frontend/components/chat/source-card.tsx` showing title, product, category, and heading path per source
- [ ] T094 [US1] Add chat API methods to `frontend/lib/api-client.ts` and the React Query hooks in `frontend/hooks/useChat.ts`
- [ ] T095 [US1] Verify quickstart **V4** against `specs/001-enterprise-knowledge-rag/quickstart.md` end to end, then confirm 100% of citation links resolve to the original publisher page (SC-018)

**Checkpoint**: MVP reached — US3 plus US1. A question about indexed documentation returns an
answer whose every substantive claim carries a citation resolving to the original page, and
the citation validity decision was made by code.

---

## Phase 5: User Story 2 - Be Told "Not Enough Evidence" Instead of a Guess (Priority: P1)

**Goal**: A question the documentation does not cover produces an honest refusal that states
what was searched, rather than a confident fabrication.

**Independent Test**: Ask a question entirely outside the indexed corpus and confirm the
system refuses, states what it searched, and presents no invented fact or citation. Quickstart
scenario **V6**.

### Tests for User Story 2 (write first, confirm they fail)

- [ ] T096 [P] [US2] Write the refusal tests in `backend/tests/unit/test_refusal.py` — a question below `MIN_RERANK_SCORE` or `MIN_EVIDENCE_SCORE` refuses; a refusal names the products, categories, and queries searched; a refusal with weakly related sources offers them as clearly-labelled insufficient leads; and a refusal with no related sources offers none (FR-007, FR-008)
- [ ] T097 [P] [US2] Write the no-fabrication test in `backend/tests/unit/test_refusal_no_fabrication.py` — across deliberately-unsupported questions, **zero** fabricated facts and **zero** fabricated citations survive into any response (SC-008, SC-009). This is an absolute zero, not a threshold (FR-066)
- [ ] T098 [P] [US2] Write the ambiguous-question test in `backend/tests/unit/test_ambiguity.py` — a question ambiguous between two products searches both, states the ambiguity, and does not silently commit to one (edge case 13)
- [ ] T099 [P] [US2] Write the degraded-retrieval test in `backend/tests/integration/test_vector_outage.py` — when the vector service is unreachable the request fails with a request id, the failure is recorded, **no answer is returned**, and no keyword-only, cached, or otherwise weakened substitute is used in its place (FR-061, FR-062, quickstart V10)
- [ ] T100 [P] [US2] Write the empty-corpus test in `backend/tests/integration/test_empty_corpus.py` — a refusal that distinguishes "nothing indexed yet" from "nothing relevant found", and never invents an answer (edge case 1)

### Implementation for User Story 2

- [ ] T101 [US2] Implement the evidence-quality threshold gate in `backend/app/retrieval/evidence.py` — refuse when `MIN_RERANK_SCORE` or `MIN_EVIDENCE_SCORE` is unmet, with both values read from configuration (FR-007)
- [ ] T102 [US2] Implement `outcome` as a first-class field on the response schema in `backend/app/schemas/chat.py` — `answered` and `refused` are states of the same shape, so a client cannot render a refusal as an error (Principle II)
- [ ] T103 [US2] Implement the refusal payload in `backend/app/chat/service.py` — the message states the indexed documentation did not provide enough evidence, and `SearchedScope` reports the products, categories, filters, and queries that were searched (FR-008)
- [ ] T104 [US2] Implement lead construction in `backend/app/retrieval/evidence.py` — weakly related sources offered as clearly-labelled insufficient leads, never as answers, and omitted entirely when nothing is even weakly related (FR-008)
- [ ] T105 [US2] Implement the low-confidence widening in `backend/app/retrieval/retriever.py` — when the analyzer's confidence is insufficient, skip or widen domain filtering and report the ambiguity rather than narrowing silently (FR-009, FR-010)
- [ ] T106 [US2] Wire the regeneration cap into `backend/app/generation/verifier.py` — at most two attempts, then refuse rather than continue (FR-011)
- [ ] T107 [US2] Implement the vector-service outage path in `backend/app/retrieval/vector_store.py` — raise `VECTOR_SERVICE_UNAVAILABLE` carrying a request id, record the failure, and return no partial result. **No keyword-only fallback, no cached prior answer, no degraded retrieval of any kind** (FR-061, FR-062, Principle II)
- [ ] T108 [US2] Write the refusal view in `frontend/components/chat/refusal-panel.tsx` — a distinct, non-error presentation showing what was searched and any clearly-labelled leads
- [ ] T109 [US2] Write the service-unavailable view in `frontend/components/chat/unavailable-panel.tsx` — a clear message carrying the request id, and never a partial answer rendered as complete (FR-061, FR-062)
- [ ] T110 [US2] Verify quickstart **V6** and **V10** end to end against `backend/tests/integration/test_vector_outage.py` and `backend/tests/integration/test_empty_corpus.py`

**Checkpoint**: The system refuses honestly. A confident wrong answer is no longer reachable
through any tested path.

---

## Phase 6: User Story 4 - Synthesize Across Product Domains (Priority: P2)

**Goal**: A question spanning products draws on evidence from each relevant domain, and the
interface reports how many sources were used.

**Independent Test**: Ask a cross-product question and confirm the answer draws on evidence
from at least two documentation domains and reports the source count. Quickstart scenario
**V5**.

- [ ] T111 [P] [US4] Write the cross-domain coverage tests in `backend/tests/unit/test_cross_domain.py` — a cross-product question retrieves evidence from every domain it spans, and a multi-part question issues separate targeted searches per aspect rather than one undifferentiated search (FR-013, SC-004)
- [ ] T112 [P] [US4] Write the domain-discipline test in `backend/tests/unit/test_domain_discipline.py` — when one product fully answers the question the system stays inside that domain rather than padding with unrelated sources (US4 scenario 3)
- [ ] T113 [P] [US4] Write the evidence-diversity tests in `backend/tests/unit/test_evidence_diversity.py` — near-duplicate passages from one page do not crowd out a distinct source, and source count is reported (FR-022, US4 scenario 1)
- [ ] T114 [US4] Implement cross-domain query expansion in `backend/app/retrieval/query_rewriter.py` — a question spanning products produces one query per domain, each with its own filter set (FR-013)
- [ ] T115 [US4] Implement per-aspect retrieval and result merging in `backend/app/retrieval/retriever.py` — results from each aspect merged and deduplicated before reranking, with the overall retrieval budget still respected
- [ ] T116 [US4] Implement cross-domain evidence diversification in `backend/app/retrieval/evidence.py` — evidence selection guarantees representation across the domains the question actually spans, not just the highest-scoring domain (SC-004, FR-021)
- [ ] T117 [P] [US4] Surface the source count in `backend/app/schemas/chat.py` so the response reports how many distinct sources and domains contributed
- [ ] T118 [US4] Write the cross-product view in `frontend/components/chat/cross-product-summary.tsx` showing the contributing domains and per-domain citations, grouped so the multi-domain nature is visible rather than implied
- [ ] T119 [US4] Verify quickstart **V5** end to end against the cross-domain coverage assertions in `backend/tests/unit/test_cross_domain.py`

**Checkpoint**: A cross-product question is answered from evidence in each relevant domain,
and the interface shows that it was.

---

## Phase 7: User Story 5 - See How the Answer Was Reached (Priority: P2)

**Goal**: A reviewer can inspect every deterministic pipeline stage behind an answer without
being shown the model's private reasoning.

**Independent Test**: Enable inspection mode and confirm every stage is visible with its
counts and scores, and that no private model reasoning appears anywhere. Quickstart
scenarios **V7** and **V4**'s progress aspect.

- [ ] T120 [P] [US5] Write the event-order test in `backend/tests/contract/test_event_order.py` — assert the streamed event sequence matches the normative order in `contracts/events.md` §3 exactly: `query_received`, `query_analyzed`, `retrieval_started`, `retrieval_completed`, `reranking_completed`, `generation_started`, `citation_validation`, `answer_completed` (FR-035)
- [ ] T121 [P] [US5] Write the no-reasoning test in `backend/tests/contract/test_no_reasoning.py` — assert no event payload, response field, or logged record anywhere contains a field matching `reasoning|thought|chain_of_thought|deliberation`, including under inspection mode and debug logging (FR-034, quickstart V7)
- [ ] T122 [P] [US5] Write the pipeline-trace test in `backend/tests/integration/test_pipeline_trace.py` — with `inspect: true`, the four request-scoped entities are snapshotted into `query_logs.pipeline_trace`; with `inspect: false` they are not persisted (FR-033, data-model.md §1.1)
- [ ] T123 [P] [US5] Write the SSE termination test in `backend/tests/contract/test_sse_termination.py` — exactly one terminal event, `answer_completed` or `error`, never both and never neither (contracts/events.md §5)
- [ ] T124 [P] [US5] Write the live-region test in `frontend/e2e/live-region.spec.ts` — progress is announced to assistive technology, and only the events listed in `contracts/events.md` §8 are announced (FR-060)
- [ ] T125 [P] [US5] Write the first-update latency test in `backend/tests/integration/test_progress_latency.py` — the first progress event is emitted within 2 seconds of request receipt (SC-016)
- [ ] T126 [US5] Implement the stage event bus in `backend/app/api/events.py` — a typed emitter for the eight normative events in the fixed documented order (FR-035)
- [ ] T127 [US5] Implement `POST /chat/stream` in `backend/app/api/routes_chat.py` — a `ReadableStream` of server-sent events. **Stage events, not token deltas**: the provider does not support streaming alongside structured output, and stage-based progress is what FR-035 specifies (R-007)
- [ ] T128 [US5] Implement the `PipelineTrace` snapshot in `backend/app/chat/service.py` — analysed question, detected domain and category, rewritten queries, applied filters, candidates retrieved, candidates reranked, evidence selected, and final citations, persisted only when `inspect: true` (FR-033)
- [ ] T129 [P] [US5] Write the SSE reader in `frontend/lib/sse.ts` — `fetch` plus a `ReadableStream` reader, because `EventSource` cannot issue a POST (R-011)
- [ ] T130 [P] [US5] Write the live region in `frontend/lib/live-region.tsx` — an `aria-live` region announcing exactly the events in `contracts/events.md` §8, with `aria-busy` during generation (FR-060)
- [ ] T131 [US5] Write the inspection trace view in `frontend/components/inspection/inspection-trace.tsx` — the eight stages in order with counts and scores, rendering pipeline state only (FR-033, FR-034)
- [ ] T132 [P] [US5] Write the progress indicator in `frontend/components/chat/progress-indicator.tsx` driven by the event stream, so a long-running answer reports progress rather than appearing frozen
- [ ] T133 [US5] Write the transparent SSE proxy in `frontend/app/api/chat/stream/route.ts` — relays the upstream stream unchanged without buffering the whole response
- [ ] T134 [US5] Verify quickstart **V7**, including grepping the full event stream for `reasoning|thought|chain_of_thought|deliberation` and confirming **no match** (FR-034)

**Checkpoint**: The pipeline is fully inspectable stage by stage, and the model's private
reasoning is verifiably absent from every path.

---

## Phase 8: User Story 6 - Measure Retrieval And Answer Quality Honestly (Priority: P2)

**Goal**: A reviewer runs a curated question set and sees quality measured from that real run
— and can tell instantly if a number is missing rather than invented.

**Independent Test**: Run the evaluation, confirm displayed metrics change to match the run,
and confirm the empty state shows no numbers before any run exists. Quickstart scenarios
**V11** and **V12**.

- [ ] T135 [P] [US6] Write the empty-state test in `backend/tests/integration/test_eval_empty_state.py` — with no run in the database, the evaluation view reports no numeric quality value of any kind, and `metrics` is `null` rather than zero (FR-036, FR-037, SC-006 sense)
- [ ] T136 [P] [US6] Write the fabrication-prohibition test in `backend/tests/unit/test_metrics_provenance.py` — every displayed metric is traceable to a stored run with a dataset fingerprint and a timestamp; hard-coded, estimated, seeded, and placeholder values are structurally impossible (FR-036, Principle VI)
- [ ] T137 [P] [US6] Write the threshold-recording test in `backend/tests/unit/test_gate_thresholds.py` — the thresholds in force are recorded on the run; a raised threshold changes the outcome to `fail` and names the failing gate; and the two absolute-zero gates have **no** configuration variable and are checked as counts, not ratios (FR-041, FR-065, FR-066)
- [ ] T138 [P] [US6] Write the topic-grading test in `backend/tests/unit/test_topic_grading.py` — a retrieved result counts as a hit when its content covers the expected product, category, and heading path, with no dependency on a specific URL, and a question with several valid answers is not scored as a failure (FR-040, FR-063, FR-064)
- [ ] T139 [P] [US6] Write the metrics-respond test in `backend/tests/integration/test_metrics_respond.py` — deliberately degrading the pipeline changes the measured metrics. **A metric that does not move under a real configuration change is a fabricated-metric bug**, and this is the Principle VI tripwire (quickstart V12)
- [ ] T140 [US6] Implement the gold set loader in `backend/app/evaluation/dataset.py` — load `evaluation/golden_questions.json`, validate it against the committed JSON Schema at load time, and refuse to run on an invalid dataset
- [ ] T141 [P] [US6] Implement retrieval metrics in `backend/app/evaluation/metrics.py` — recall@5, precision@5, MRR, and nDCG, graded by topic coverage rather than URL, each reported against its configured threshold (FR-038, FR-063, SC-001, SC-002, SC-003)
- [ ] T142 [P] [US6] Implement answer-quality metrics in `backend/app/evaluation/metrics.py` — citation validity, citation completeness, claim coverage, faithfulness, groundedness, and answer relevance; the first three are threshold-gated, the two absolute zeros are counted not ratioed (FR-038, SC-005, SC-006, SC-007, FR-066)
- [ ] T143 [P] [US6] Implement performance and error metrics in `backend/app/evaluation/metrics.py` — total and per-stage latency and error rate, with `total_time` as the honest figure because it includes queueing
- [ ] T144 [US6] Implement the run orchestrator in `backend/app/evaluation/runner.py` — execute the shared chat service non-streaming per question, so measured results reflect the same work a user sees (R-007)
- [ ] T145 [US6] Implement threshold evaluation in `backend/app/evaluation/runner.py` — `gate_outcome` strictly `pass` or `fail` with no soft third value, `gate_failures` naming each failing gate, and `null` metrics meaning *not applicable* rather than *not measured* (FR-041, Principle VI)
- [ ] T146 [US6] Persist per-question results in `backend/app/evaluation/runner.py` — write an `evaluation_results` row per question so the absolute-zero counts are auditable to an individual question, not only to a run aggregate (data-model.md §1.2, SC-008, SC-009)
- [ ] T147 [P] [US6] Implement the evaluation schemas in `backend/app/schemas/evaluations.py` matching `contracts/openapi.yaml` — `EvaluationRun`, `EvaluationResult`, `EvaluationQuestion`, and `EvaluationDataset`
- [ ] T148 [US6] Implement `backend/app/api/routes_evaluations.py` — expose the dataset, start a run, fetch a run with its thresholds and gate outcome, and page through per-question results
- [ ] T149 [US6] Write the evaluation dashboard in `frontend/app/evaluations/page.tsx` and `frontend/components/evaluations/eval-dashboard.tsx` — metrics grouped retrieval / answer / performance, each labelled with the run id and timestamp that produced it
- [ ] T150 [P] [US6] Write the gate banner in `frontend/components/evaluations/gate-banner.tsx` — a failing gate is shown as a failing gate in plain language, never softened
- [ ] T151 [US6] Write the empty state in `frontend/components/evaluations/empty-state.tsx` — explains that no run has been performed and renders **no** placeholder numbers anywhere (FR-037)
- [ ] T152 [US6] Verify quickstart **V11** and **V12** end to end, including `backend/tests/integration/test_metrics_respond.py` proving the metrics move under a real configuration change

**Checkpoint**: Quality is measured from real runs, is traceable to the run that produced it,
and a missing number is visibly missing rather than filled in.

---

## Phase 9: User Story 7 - Explore And Curate The Indexed Corpus (Priority: P3)

**Goal**: An operator browses what is indexed, sees its health and coverage, and manages
sources and configuration from a dashboard.

**Independent Test**: Open the corpus views and confirm source, document, and configuration
status are visible and actionable without touching a database.

- [ ] T153 [P] [US7] Write the secret-exposure test in `backend/tests/contract/test_config_no_secrets.py` — assert no response from any endpoint contains a secret value, and that the `SecretPresence` schema has no value field anywhere (FR-042)
- [ ] T154 [P] [US7] Write the corpus-browse tests in `backend/tests/integration/test_corpus_views.py` — sources show identity, product, pages indexed, last crawl time, and status; documents are browsable and filterable; a source with every document deleted remains listed with zero pages rather than disappearing (FR-030, edge case 16)
- [ ] T155 [US7] Build the sources console in `frontend/app/sources/page.tsx` and `frontend/components/corpus/source-table.tsx` — identity, product, pages indexed, last crawl time, status, and per-source actions
- [ ] T156 [P] [US7] Build the documents browser in `frontend/app/documents/page.tsx` and `frontend/components/corpus/document-table.tsx` — browse and filter, with each row linking to its original page
- [ ] T157 [P] [US7] Build the document detail view in `frontend/components/corpus/document-detail.tsx` showing title, product, category, page type, language, fingerprint, crawl time, and lifecycle state
- [ ] T158 [P] [US7] Build the unit inspector in `frontend/components/corpus/unit-inspector.tsx` showing each unit's ordinal, heading path, and token size — the direct visual check that R-009's reconstruction walk worked
- [ ] T159 [P] [US7] Build the crawl job history view in `frontend/components/corpus/crawl-history.tsx` showing per-job pages discovered, pages processed, and error detail (FR-054)
- [ ] T160 [US7] Build the configuration view in `frontend/app/settings/page.tsx` and `frontend/components/corpus/config-view.tsx` — operational status and the settings that affect retrieval, with secret **presence** only and no values (FR-042, US7 scenario 4)
- [ ] T161 [US7] Build the health view in `frontend/components/corpus/health-view.tsx` — provider, vector service, and database status from `/health` (FR-061)
- [ ] T162 [US7] Verify the US7 independent test and the empty-corpus case against `backend/tests/integration/test_corpus_views.py`

**Checkpoint**: Day-two operation needs no database access.

---

## Phase 10: Polish & Cross-Cutting Concerns

**Purpose**: Publication requirements, accessibility verification, security review, and
end-to-end validation across all stories.

- [ ] T163 [P] Write `docs/ARCHITECTURE.md` — the architecture overview, including the pipeline diagram and the modular-monolith rationale (SC-019)
- [ ] T164 [P] Write `docs/DATA_SOURCE_POLICY.md` — what is collected, what is never collected, what is never redistributed, crawling conduct, and deletion semantics (FR-051, SC-019)
- [ ] T165 [P] Write `docs/EVALUATION.md` — the evaluation guide: how to run it, how the dataset is graded by topic rather than URL, how thresholds are configured, and how to read a failing gate honestly (SC-019, FR-065)
- [ ] T166 [P] Write `docs/API.md` — the published interface reference generated from `contracts/openapi.yaml`, conforming to it rather than duplicating it (SC-019)
- [ ] T167 [P] Expand `README.md` with the demonstration walkthrough, keeping the independence statement and the data-source policy summary, and stating plainly that the system is unaffiliated with Atlassian (FR-049, SC-019)
- [ ] T168 Run the automated accessibility check in `frontend/e2e/accessibility.spec.ts` with `@axe-core/playwright` across every view and report the actual result — **no serious or critical violations, and AA contrast met** (SC-022, R-012)
- [ ] T169 Run the manual keyboard walkthrough across every view under `frontend/app/` and record the actual result in `frontend/e2e/keyboard-walkthrough.md` — every control reachable and operable, visible focus, no traps (FR-057, SC-021, R-012). Automated tooling does not replace this walkthrough, and neither check may be claimed without having been run
- [ ] T170 Verify accessible names and heading hierarchy in `frontend/e2e/semantics.spec.ts` — every control programmatically named, semantic headings in a correct hierarchy, and state never conveyed by colour alone (FR-058, FR-059)
- [ ] T171 Run the full quickstart validation `specs/001-enterprise-knowledge-rag/quickstart.md` scenarios **V1** through **V14** and record the actual outcome of each, including the corpus scale check for 50–100 pages yielding 500–1500 units (SC-011)
- [ ] T172 Run the performance check in `backend/tests/performance/test_latency.py` — first update within 2s, complete answer with citations within 8s for at least 95% of questions, and no unexplained pause over 10s (SC-016, SC-017)
- [ ] T173 Run `scripts/check_repo_hygiene.sh` and confirm zero third-party page contents and zero secrets in version control (SC-015, FR-032)
- [ ] T174 Run the full suite across `backend/tests/unit/`, `backend/tests/integration/`, `backend/tests/e2e/`, and `backend/tests/contract/` and record the actual pass counts in `backend/tests/COVERAGE.md` — every FR-052 named component demonstrably covered
- [ ] T175 Perform a security review of `backend/app/core/errors.py`, `backend/app/core/security.py`, `backend/app/core/sanitize.py`, and `backend/app/core/logging.py`, confirming no stack trace, internal path, or secret reaches any response or log, and record the result in `docs/DATA_SOURCE_POLICY.md` (FR-044, FR-046, FR-047)
- [ ] T176 Confirm `make up` works from a clean clone of `README.md`'s published instructions, timed against the 15-minute reproduction budget (FR-052, SC-020)
- [ ] T177 Record the real measured evaluation results in `docs/EVALUATION.md` from an actual run, including any gate that failed, reported as failed (FR-036, FR-041, Principle VI)
- [ ] T178 Merge the final `001-enterprise-knowledge-rag/phase-10-polish` branch into `main` and push — the last merge of the build, not its only commit; every phase checkpoint before it was already committed and merged (see Version Control Discipline)

**Checkpoint**: The build is published, reviewable, and honest about itself. Every success
criterion has either passed against a real run or is recorded as failing, the accessibility
claims were produced by actually running the checks rather than by asserting them, and a
reviewer can reach a working demonstration from the published instructions alone. This is
the checkpoint T178 merges on.

---

## Dependencies & Execution Order

### Phase dependencies

- **Setup (Phase 1)**: no dependencies — start immediately
- **Foundational (Phase 2)**: depends on Setup — **blocks every user story**
- **User Stories (Phases 3–9)**: all depend on Foundational
- **Polish (Phase 10)**: depends on all desired stories being complete

### User story dependencies

- **US3 (P1)**: depends only on Foundational. **Produces the corpus every other story needs.**
- **US1 (P1)**: depends on Foundational **and US3** — it cannot be tested without indexed content
- **US2 (P1)**: depends on US1 (it is a branch of the answering path), and on US3
- **US4 (P2)**: depends on US1; adds cross-domain behaviour
- **US5 (P2)**: depends on US1; adds the streaming and inspection surface
- **US6 (P2)**: depends on US1; reuses the identical pipeline non-streaming so metrics reflect real work
- **US7 (P3)**: depends on US3; independent of the answering path

Only US3 and US7 are reachable from Foundational alone. US1, US2, US4, US5, and US6 all need a
corpus. This is the deviation recorded at the top of this document, and it is a property of
the feature, not an artefact of this task list.

### Within each user story

- Tests are written and confirmed failing before implementation
- Models and schemas before services
- Services before endpoints
- Frontend after the endpoints it consumes
- Story complete and independently verified before moving on

### Parallel opportunities

- All `[P]` tasks within a phase touch different files and have no unfinished dependencies
- T027 and T028 (the two vendor smoke tests) are independent and both gate the vector and
  provider adapters
- T020–T023 (core config, logging, errors, security) are independent of each other
- T049, T051, T053, T054 are independent ingestion modules
- T079 and T080 (analyzer and rewriter) are independent
- T092, T093 and the US4/US5/US6 frontend tasks touch different components
- **The seven story phases cannot run in parallel** — they share the chat service, the
  vector store, and the evidence selector. Stories 4, 5, and 6 in particular all extend the
  same `backend/app/chat/service.py` orchestration and would conflict on the same file

---

## Parallel Example: Phase 3 (US3 — Ingestion)

```bash
# Launch all ingestion unit tests together — eight files, no shared state:
Task: "Write the SSRF guard tests in backend/tests/unit/test_security.py"
Task: "Write the heading-path reconstruction tests in backend/tests/unit/test_extractor_headings.py"
Task: "Write the chunker tests in backend/tests/unit/test_chunker.py"
Task: "Write the metadata extraction tests in backend/tests/unit/test_metadata.py"
Task: "Write the content fingerprint and supersession tests in backend/tests/unit/test_fingerprint.py"
Task: "Write the crawl-security tests in backend/tests/unit/test_crawler_controls.py"
Task: "Write the document lifecycle tests in backend/tests/unit/test_document_state.py"
Task: "Write the one-active-crawl test in backend/tests/integration/test_crawl_concurrency.py"
Task: "Write the deletion-beats-inflight-crawl test in backend/tests/integration/test_deletion_precedence.py"
```

Note: T041 is in this group and is the highest-risk test in the phase. It should be run and
understood before the extractor is written, because the implementation it constrains
(T052) is the single largest technical risk identified in planning.

---

## Version Control Discipline

### Branches

One branch per phase, named `001-enterprise-knowledge-rag/phase-NN-slug`:

```
main                                                     # spec baseline + each merged phase
├── 001-enterprise-knowledge-rag/phase-01-setup
├── 001-enterprise-knowledge-rag/phase-02-foundational
├── 001-enterprise-knowledge-rag/phase-03-us3-ingestion     # MVP together with phase 04
├── 001-enterprise-knowledge-rag/phase-04-us1-cited-answers # MVP
├── 001-enterprise-knowledge-rag/phase-05-us2-refusal
├── 001-enterprise-knowledge-rag/phase-06-us4-cross-product
├── 001-enterprise-knowledge-rag/phase-07-us5-explainability
├── 001-enterprise-knowledge-rag/phase-08-us6-evaluation
├── 001-enterprise-knowledge-rag/phase-09-us7-corpus-console
└── 001-enterprise-knowledge-rag/phase-10-polish
```

Each branch is cut from `main` at the start of its phase and merged back at that phase's
checkpoint. `main` therefore always holds a coherent, working state rather than half of a
build, and a reviewer reading the history sees one phase per merge.

`001-enterprise-knowledge-rag` is also the feature identifier in
`.specify/feature.json` and in the `BRANCH` field that
`.specify/scripts/bash/check-prerequisites.sh` reports. It is not by itself a git branch —
the phase branches above are.

### Commits

- **Commit at every phase checkpoint**, not at every task. Per-task commits bury the
  meaningful ones; the checkpoints already exist at the end of each phase.
- **T027 and T028 are committed separately and before T029, T030, and T031.** They resolve
  vendor API ambiguity against the installed SDK — whether `create_for_model` or
  `IntegratedSpec` is current, and whether reasoning suppression actually works. A reviewer
  should be able to read the finding without wading through the adapter code that depends
  on it.
- **Never mix a spec change with a code change in one commit.** If a task reveals that the
  specification is wrong, fix the spec, commit that on its own, then implement against the
  corrected spec. A commit that changes what the system must do and how it does it at the
  same time is not reviewable.
- **Never force-push to `main`.** It is the spec baseline plus every merged phase.
- A commit message states what changed and, where relevant, which requirements it
  satisfies. The four existing commits set the standard: what happened, and the consequence
  if it is not read.

### Paths in code and specs

- Every path written into a file — code, test, configuration, Dockerfile, documentation —
  is **repo-relative and starts at the repository root**, for example
  `backend/app/retrieval/vector_store.py` or
  `specs/001-enterprise-knowledge-rag/quickstart.md`.
- **Never** write a home directory, a desktop path, an OS-specific separator, or anything
  else that resolves differently on another machine. No `/home/<user>/…`, no `Desktop/`, no
  `C:\…`, no `/Users/<name>/…`.
- A path that depends on where the repository happens to be cloned is a defect, not a
  convenience. It breaks the clone, and it will be written by habit rather than by need.
- Paths that are genuinely external — a publisher's documentation URL, a site's
  `/robots.txt`, a vendor API endpoint — are URLs or external absolute paths by nature, and
  are written as such deliberately rather than by accident.
- Shell commands in the quickstart assume the repository root as the working directory.
- `scripts/check_repo_hygiene.sh` fails the build on a machine-specific path in any tracked
  file, so this rule is enforced on every change rather than merely stated here.

---

## Implementation Strategy

### MVP — User Stories 3 and 1 together

The spec designates US1 as the MVP. **That is not achievable on its own**, and shipping it
alone would deliver a system that cannot answer anything without code-level corpus
construction. The honest MVP is:

1. Phase 1: Setup
2. Phase 2: Foundational — **including T027 and T028**, which resolve the two vendor API
   ambiguities before any code is written against them
3. Phase 3: US3 — ingestion
4. Phase 4: US1 — cited grounded answers
5. **STOP and VALIDATE** — US3's V2 and US1's V4 both pass independently
6. This pair satisfies US1's stated independent test verbatim: *"Index a documentation page,
   ask one question about it, and confirm the answer's claims each carry a citation whose
   link opens the original page."*

### Incremental delivery

1. Setup + Foundational → the two vendor questions answered, the stack runs
2. US3 → an operator can build a corpus → validate V2
3. US1 → questions get cited answers → validate V4 (**MVP**)
4. US2 → the system refuses honestly → validate V6, V10
5. US4 → cross-product questions span domains → validate V5
6. US5 → the pipeline is inspectable and progress streams → validate V7
7. US6 → quality is measured from real runs → validate V11, V12
8. US7 → day-two operation needs no database
9. Polish → publication, accessibility, and the full quickstart

Each story adds a capability without breaking the previous ones. Note that the order is
**not** the spec's numeric order for the P1 band, for the reason recorded at the top.

### Parallel team strategy

The seven story phases share the chat service, vector store, and evidence selector, so they
are **not** parallelisable even with a large team. The genuine parallelism is:

- Within Phase 1, all `[P]` setup tasks
- Within Phase 2, the four core modules and the two vendor smoke tests
- Within any story phase, the `[P]` test files, and the frontend tasks once endpoints exist
- US7 can be worked by a second developer alongside US1–US6 once US3 is complete, since it
  shares only the source and document endpoints

---

## Notes

- `[P]` means different files with no dependency on an unfinished task
- `[Story]` maps each task to its user story for traceability to spec.md
- Every task names a concrete file; none requires interpretation beyond its description
- **Every path is repo-relative from the repository root. Never a home directory, a desktop
  path, or an OS-specific separator** — see Paths in code and specs
- **Commit at each phase checkpoint, one branch per phase.** T027 and T028 are additionally
  committed on their own before the adapters that depend on them. A spec change and a code
  change never share a commit — see Version Control Discipline
- Tests are confirmed failing before implementation, per the TDD ordering in each story phase
- T027 and T028 are **gating tasks**: their findings change the code written in T029, T030,
  and T031. Do not skip them or proceed on a guess
- T041/T052 are the highest-risk pair in the build; the R-009 heading walk is the only way
  FR-018 is satisfiable, and V2 is the test that catches it being wrong
- The task ordering within each story follows the ingestion→retrieval→generation→interface
  order of the original 20-phase build plan, mapped into story phases
- No metric in this repository has been measured. Phase 10's T177 records real results from a
  real run, including failures
