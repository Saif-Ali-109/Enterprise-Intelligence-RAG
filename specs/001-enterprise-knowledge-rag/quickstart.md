# Quickstart & Validation Guide

How to stand the system up and prove, scenario by scenario, that it does what
[spec.md](./spec.md) claims. Every scenario names the requirement and success criterion it
verifies, gives the commands, and states the expected outcome so a result can be judged
rather than assumed.

This is a **run and verify** guide. It deliberately contains no service implementations,
migrations, or test bodies — those belong to `tasks.md` (produced by `/speckit.tasks`, not
yet written) and the implementation phase. For what the payloads mean, see
[contracts/](./contracts/); for what is stored, see [data-model.md](./data-model.md).

---

## 1. Prerequisites

| Requirement | Version | Why |
|---|---|---|
| Docker + Compose v2 | current | Single-command environment (FR-052, SC-020) |
| Python | 3.12+ | Backend runtime |
| Node.js | 20.9+ | Next.js 16 requirement |
| TypeScript | 5.1+ | Next.js 16 requirement |
| PostgreSQL client tools | 16 | Inspecting the registry directly |
| A Groq API key | — | `GROQ_API_KEY` |
| A Pinecone API key and index entitlement | — | `PINECONE_API_KEY` |

Two service credentials are genuinely required. There is no offline mode: Principle XI and
FR-054 of the brief's intent forbid substituting a local vector store for the hosted one,
because a faked vector service would make the quality gates meaningless.

**Before the first crawl**, confirm the documentation paths you intend to register actually
permit crawling, by reading the publisher's stated directives at `/robots.txt`. Paths that
disallow crawling are excluded from the manifest — not worked around (FR-028).

## 2. Configuration

Copy `.env.example` to `.env` and fill in the two keys. Every setting is environment-driven;
nothing is hard-coded in source (Principle VII).

| Variable | Required | Default | Notes |
|---|---|---|---|
| `GROQ_API_KEY` | yes | — | Never reaches the browser, logs, or Git (FR-042) |
| `PINECONE_API_KEY` | yes | — | Same |
| `PINECONE_INDEX_NAME` | no | `enterprise-knowledge` | |
| `PINECONE_NAMESPACE` | no | `atlassian-public` | Reserved key; never stored as metadata (R-003) |
| `PINECONE_EMBED_MODEL` | no | `llama-text-embed-v2` | **See R-001.** Omit `dimension` — it is read from service config |
| `PINECONE_RERANK_MODEL` | no | `bge-reranker-v2-m3` | Reranker interface is unchanged if swapped |
| `GROQ_MODEL` | no | `openai/gpt-oss-120b` | Do **not** use `llama-3.3-70b-versatile`; it is Enterprise-only and returns 401 (R-006) |
| `GROQ_CLASSIFICATION_MODEL` | no | `openai/gpt-oss-20b` | 1,000 t/s; 1,000 tok/s matters for the 8s budget |
| `DATABASE_URL` | no | compose service | |
| `ALLOWED_DOMAINS` | no | Atlassian documentation hosts | Crawl may not leave this set (FR-027) |
| `CRAWL_MAX_PAGES` / `CRAWL_MAX_DEPTH` / `CRAWL_DELAY_SECONDS` | no | 100 / 2 / 1.0 | Bounded crawl (FR-027) |
| `RETRIEVAL_CANDIDATE_POOL` | no | 12 | Initial retrieval budget |
| `RETRIEVAL_RERANK_TOP_N` | no | 6 | After reranking |
| `EVIDENCE_MIN_UNITS` / `EVIDENCE_MAX_UNITS` | no | 3 / 6 | Focused evidence set (FR-020) |
| `MIN_RERANK_SCORE` / `MIN_EVIDENCE_SCORE` | no | tuned | Below these, the system refuses (FR-007) |
| `GENERATION_MAX_ATTEMPTS` | no | 2 | Hard cap (FR-011) |
| `QUESTION_MAX_LENGTH` | no | 2000 | Bounded input (FR-043) |
| `EVAL_GATE_RECALL_AT_5` | no | `0.80` | Configurable threshold (FR-065) |
| `EVAL_GATE_MRR` | no | `0.75` | Configurable threshold |
| `EVAL_GATE_PRECISION_AT_5` | no | `0.70` | Configurable threshold |
| `EVAL_GATE_CITATION_VALIDITY` | no | `0.90` | Configurable threshold |
| `EVAL_GATE_CLAIM_COVERAGE` | no | `0.85` | Configurable threshold |
| `EVAL_GATE_FAITHFULNESS` | no | `0.90` | Configurable threshold |
| `EVAL_GATE_CROSS_PRODUCT_COVERAGE` | no | `0.90` | Configurable threshold |
| `EVAL_GATE_UNSUPPORTED_REFUSAL_RATE` | no | `1.0` | Configurable |

The two absolute-zero gates — zero fabricated facts, zero invalid citation identifiers —
have **no configuration variable**, and that is deliberate. FR-066 excludes them from
adjustment, so there is nothing to tune and nothing to relax. Anything in `.env` resembling a
threshold for them is a mistake and should be removed.

Confirm no key is exposed before proceeding:

```bash
curl -s localhost:8000/api/v1/config | jq '.secrets, .generation.reasoning_exposed'
```

**Expected**: every secret shows `{"configured": true}` with **no value field**, and
`reasoning_exposed` is `false`. A value here is a FR-042 failure — stop and fix it.

## 3. Start

```bash
docker compose up --build
```

The stack is: PostgreSQL, the backend on `:8000`, the dashboard on `:3000`.

```bash
curl -s localhost:8000/api/v1/health | jq
```

**Expected**: `status: "ok"`, and `database`, `vector_store`, `language_model` each report
`ok`. The three are reported independently on purpose — a vector outage must be visible as a
vector outage, not as a generic failure.

---

## 4. Validation scenarios

Run in order. Later scenarios assume earlier ones have passed.

### V1 — Address guard refuses internal targets before connecting

**Verifies** FR-044, SC-014, FR-027.

```bash
for u in "http://127.0.0.1:5432/" "http://169.254.169.254/latest/meta-data/" \
         "http://10.0.0.5/" "http://[::1]/" "file:///etc/passwd" "http://localhost:8000/"; do
  printf '%-42s ' "$u"
  curl -s -o /dev/null -w '%{http_code} ' -X POST localhost:8000/api/v1/sources \
    -H 'Content-Type: application/json' \
    -d "{\"name\":\"t\",\"start_url\":\"$u\"}"
  curl -s -X POST localhost:8000/api/v1/sources -H 'Content-Type: application/json' \
    -d "{\"name\":\"t\",\"start_url\":\"$u\"}" | jq -r '.error.code // "no error"'
done
```

**Expected**: `SSRF_BLOCKED` for every entry, or `INVALID_URL` for the `file://` scheme.
Every target is refused **before** a connection is opened — verify with
`docker compose logs backend | grep -c "connect"` returning no new connection attempts for
those hosts. Link-local `169.254.169.254` matters most: it is the cloud metadata service, and
reaching it is the classic SSRF escalation.

Then confirm a legitimate registration is accepted, and that a disallowed path is *skipped
and recorded* rather than fetched by another route:

```bash
curl -s localhost:8000/api/v1/crawl-jobs | jq '.items[0].skipped_reasons'
```

**Expected**: a `robots_disallowed` count where applicable, with no fetch of that path.

### V2 — Register a source and see it indexed with correct provenance

**Verifies** FR-024, FR-030, SC-010, User Story 3.

```bash
curl -s -X POST localhost:8000/api/v1/sources -H 'Content-Type: application/json' \
  -d '{"name":"Jira Software docs","start_url":"<registered entry point>",
       "product_domain":"jira","max_pages":60,"max_depth":2}' | jq
```

Poll the returned job until `status` is `completed` or `completed_with_errors`, then:

```bash
curl -s "localhost:8000/api/v1/documents?state=indexed&limit=5" | jq '.items[] | {title, product, category, state, vectors_live, unit_count}'
```

**Expected**: every document has a non-empty `title`, a `product` where detectable, `state:
"indexed"`, `vectors_live: true`, and a `source_url` that opens the original publisher page.
`product` may legitimately be `null` — FR-009 forbids forcing a classification, so a `null`
here is correct behaviour, not a defect.

```bash
curl -s "localhost:8000/api/v1/documents/<id>/units?limit=8" | jq '.items[] | {ordinal, heading_path, block_types, token_count}'
```

**Expected**: non-empty `heading_path` per unit (FR-018), `block_types` showing real
structure — `code` and `table` present where the documentation has them — and ordinals
contiguous from 0. This is the check that proves chunking followed document structure
(FR-015) rather than a fixed character count. A corpus of uniformly-sized units with
identical heading paths and no `code` blocks means structure-aware chunking is not working.

### V3 — Re-crawling adds no duplicates

**Verifies** FR-026, SC-012, User Story 3 scenario 2.

```bash
before=$(curl -s "localhost:8000/api/v1/documents?limit=1" | jq '.total')
curl -s -X POST localhost:8000/api/v1/sources/<source_id>/crawl
# wait for completion
after=$(curl -s "localhost:8000/api/v1/documents?limit=1" | jq '.total')
echo "documents before=$before after=$after"
curl -s localhost:8000/api/v1/crawl-jobs/<job_id> | jq '.counters, .skipped_reasons'
```

**Expected**: `before == after`, and `counters.unchanged` equals the page count with
`counters.processed` at 0 or near it. A growing document count means fingerprint-based
skipping is broken.

### V4 — Ask a question and get a cited answer

**Verifies** FR-001…FR-006, SC-018, User Story 1. **The core promise — do not skip this.**

```bash
curl -s -N -X POST localhost:8000/api/v1/chat/stream \
  -H 'Content-Type: application/json' -H 'Accept: text/event-stream' \
  -d '{"question":"How do I create a JQL filter in Jira?","inspect":true}' | tee /tmp/stream.txt
```

`-N` disables curl's buffering — without it events arrive in a clump at the end and you will
diagnose your own code wrongly (R-011).

**Expected**, in this exact order: `query_received`, `query_analyzed`, `retrieval_started`,
`retrieval_completed`, `reranking_completed`, `generation_started`, `citation_validation`,
`answer_completed`, then `data: [DONE]`. All eight present, none skipped.

```bash
jq -r 'select(.data.source_url) | .data.source_url' /tmp/stream.txt | while read -r u; do
  printf '%s -> ' "$u"; curl -s -o /dev/null -w '%{http_code}\n' -L "$u"
done
```

**Expected**: every citation URL returns `200` after following redirects (SC-018). Then open
one `quote` from the stream and confirm the text appears in the linked page — SC-005 requires
the cited span to genuinely support the claim, not merely to exist.

For conditional instructions, confirm the answer keeps the source's conditional phrasing and
does not flatten "if you have admin permissions, you can…" into "you can…" (FR-006).

### V5 — Cross-product question spans domains

**Verifies** FR-013, SC-004, User Story 4.

```bash
curl -s -N -X POST localhost:8000/api/v1/chat/stream \
  -H 'Content-Type: application/json' -H 'Accept: text/event-stream' \
  -d '{"question":"Why can a user see a linked page in Confluence but not in Jira?","inspect":true}'
```

**Expected**: `query_analyzed` reports **more than one** entry in `rewrite_queries` and a
non-null `ambiguity_note`; `answer_completed` cites evidence from **at least two** `product`
values. One undifferentiated query and one product's sources means cross-product expansion is
not happening.

The inverse also matters — ask a question a single product fully answers and confirm the
answer stays in that domain rather than padding with unrelated sources (User Story 4
scenario 3).

### V6 — An unanswerable question is refused, not guessed

**Verifies** FR-007…FR-011, SC-008, User Story 2. **The most important trust behaviour.**

```bash
curl -s -N -X POST localhost:8000/api/v1/chat/stream \
  -H 'Content-Type: application/json' -H 'Accept: text/event-stream' \
  -d '{"question":"What is the recommended salary band for a Jira administrator in Berlin?","inspect":true}'
```

**Expected**: all eight events still fire, then `answer_completed` with
`outcome: "refused"`, `answer: null`, `refusal_reason: "INSUFFICIENT_EVIDENCE"`, a populated
`searched` block naming the queries and products actually searched, and `citations: []`.
Any `leads` present must each carry `insufficient: true`.

**Failure modes to reject explicitly**: a non-null `answer`; any entry in `citations`; a
fabricated `source_url`. SC-008 demands zero fabricated facts *and* zero fabricated
citations, and this is the scenario that catches a regression there.

Then test the borderline case — a question ambiguous between two products:

**Expected**: low per-field confidence in `query_analyzed`, `filters_suppressed: true`, a
broader search rather than a narrowed one (FR-010), and a `null` `detected_product` rather
than a forced guess (FR-009).

### V7 — Inspection mode, and no reasoning anywhere

**Verifies** FR-033, FR-034, SC-021, User Story 5.

```bash
grep -Eio '"(reasoning|thought|chain_of_thought|thoughts|deliberation|scratchpad)"' /tmp/stream.txt
```

**Expected**: no output. The default generation models are reasoning models whose API can
return raw reasoning unless explicitly suppressed (R-014), so this grep is a real check, not
a formality.

With `inspect: true`, the response carries a `trace` containing the analysis, every retrieved
candidate with its similarity score, every reranked candidate with rank and rerank score, the
selected evidence with its per-signal selection scores, and the validated citations. `trace`
is `null` without `inspect` — the full trace is captured only on request, not by default.

Confirm the trace is real rather than reconstructed: `candidates_retrieved` in
`retrieval_completed` must equal the length of `trace.stages.retrieval.candidates`, and
`citations_valid + citations_stripped + citations_repaired` must equal
`trace.stages.citations.entries` length plus stripped ones.

### V8 — Deleting removes content from every layer

**Verifies** FR-031, SC-013, FR-056.

```bash
curl -s -X DELETE localhost:8000/api/v1/documents/<document_id>   # 204
curl -s localhost:8000/api/v1/documents/<document_id> | jq '{state, vectors_live, tombstoned_at}'
curl -s -X DELETE localhost:8000/api/v1/documents/<document_id>   # 204 again — idempotent
```

**Expected**: `state: "deleted"`, `vectors_live: false`, `tombstoned_at` set, and the second
delete also returning `204`.

Then ask a question whose answer depended on that document and confirm the system either
refuses or answers without it — **not** that it serves stale content (SC-013). This is the
check that vectors were actually removed, not merely marked deleted in Postgres.

### V9 — Concurrent crawl is rejected, and deletion beats an in-flight crawl

**Verifies** FR-055, FR-056, User Story 3 edge cases.

```bash
curl -s -X POST localhost:8000/api/v1/sources/<source_id>/crawl -o /dev/null   # start one
curl -s -X POST localhost:8000/api/v1/sources/<source_id>/crawl | jq           # immediately
```

**Expected**: `409` with `code: "CRAWL_ALREADY_RUNNING"` and a message **naming the running
job id**. The first crawl continues undisturbed. This is enforced by a partial unique index,
not only by application logic — verify in the database:

```sql
SELECT indexdef FROM pg_indexes WHERE indexname = 'crawl_jobs_one_active_per_source';
```

For the deletion race: start a crawl, delete a document mid-flight, and confirm the document
ends `deleted` with `vectors_live: false`. Deletion wins; content must not reappear.

### V10 — Vector service outage produces a clean failure

**Verifies** FR-061, FR-062, SC-008, User Story 2 edge cases. **The degradation test.**

```bash
docker compose stop backend
# restart with a deliberately wrong Pinecone key so startup proceeds but search fails
PINECONE_API_KEY=invalid docker compose up backend
curl -s -X POST localhost:8000/api/v1/chat -H 'Content-Type: application/json' \
  -d '{"question":"How do I create a JQL filter?"}' | jq
```

**Expected**: `503` with `code: "VECTOR_SERVICE_UNAVAILABLE"` and a `request_id`. Explicitly
**absent**: any `answer`, any `citations`, any partial result, any cached prior answer, any
keyword-matched substitute. FR-062 prohibits all four, and a degraded answer that looks
complete is the specific failure this requirement exists to prevent.

Also confirm the failure was recorded (FR-061) and that `GET /health` now reports
`vector_store: "unavailable"` independently of the other dependencies.

### V11 — Evaluation: empty state first, then real numbers

**Verifies** FR-036…FR-041, SC-001…SC-009, User Story 6.

**Before any run exists:**

```bash
curl -s localhost:8000/api/v1/evaluations/runs | jq
```

**Expected**: `{"items": [], "total": 0, ...}` and an empty state in the dashboard
explaining that no results exist yet. **No placeholder numbers, no zeroes presented as
measurements, no example figures.** FR-037 is explicit and SC-006 of the spec's honesty
requirement depends on it.

**Then run it:**

```bash
curl -s -X POST localhost:8000/api/v1/evaluations/runs | jq
# poll until status == "completed"
curl -s localhost:8000/api/v1/evaluations/runs/<run_id> | jq '{metrics, thresholds, gate_outcome, gate_failures}'
```

**Expected**: every metric in `metrics` was computed from this run and matches
`gate_outcome` — a value below its threshold **must** appear in `gate_failures` and the
outcome must be `fail`. FR-041 forbids reporting a failing run as anything softer. Check the
absolute zeros specifically:

```bash
curl -s localhost:8000/api/v1/evaluations/runs/<run_id>/results | \
  jq '[.items[] | select(.fabricated_fact_count > 0 or .invalid_citation_count > 0)] | length'
```

**Expected**: `0` (SC-008, SC-009).

Also confirm nulls are honest: `recall_at_5` is `null` for `is_unsupported` questions, not
`0.0`. A `0.0` there would drag the average down and misrepresent a correct refusal as a
retrieval failure.

Verify traceability: `dataset_version`, `dataset_fingerprint`, and `config_snapshot` on the
run must match the dataset and settings actually used, and `started_at`/`finished_at` must
bracket the run (FR-036, User Story 6 scenario 3).

Then confirm threshold provenance (FR-065, FR-066):

```bash
curl -s localhost:8000/api/v1/evaluations/runs/<run_id> | jq '{thresholds, gate_outcome}'
grep -E 'FABRICAT|INVALID_CITATION|GATE.*ZERO' .env   # expected: no output
```

**Expected**: the `thresholds` recorded on the run equal the `EVAL_GATE_*` values in force when
it executed, and `gate_outcome` is consistent with them. The second command returns nothing —
the absolute-zero gates have no threshold and no knob. A `thresholds` block that omits them
is correct, not incomplete.

**Governance check.** Re-run with a deliberately raised threshold (for example
`EVAL_GATE_RECALL_AT_5=0.99`) and confirm the run reports `fail` with that gate listed in
`gate_failures`. Then confirm the opposite direction is not available: no setting exists that
would make a run with a fabricated fact report `pass`, because the zero is checked as a
boolean count and not as a ratio (FR-065 last clause, FR-066).

### V12 — Metrics move when the pipeline changes

**Verifies** that displayed metrics are measured, not decorative (FR-036, User Story 6
scenario 1).

Temporarily set `RETRIEVAL_RERANK_TOP_N=1` and re-run. **Expected**: `recall_at_5` and `mrr`
change, and plausibly fall — a top-1 cut cannot satisfy recall@5. Metrics that do **not** move
in response to a change that must affect them mean the numbers are not being computed from the
run. Restore the setting and re-run to confirm they move back.

### V13 — Accessibility

**Verifies** FR-057…FR-060, SC-021, SC-022.

Automated: run the project's axe-core pass over each view.

**Expected**: no serious or critical violations, and all text at AA contrast. Tailwind v4
supplies no contrast tokens, so contrast is an explicit check rather than an inherited default
(R-012).

Manual keyboard walk — required, because no automated tool covers everything:

- Tab from page load reaches every interactive control, in a sensible order, with a visible
  focus ring on each.
- No trap: `Shift+Tab` back out of the chat composer and evidence panel always works.
- Every control has a programmatically determinable name; icons are not unlabelled.
- Heading levels are sequential with no skipped level.
- **With a screen reader, ask a question and confirm progress is announced** through the live
  region (FR-060) — not merely rendered. Announcing is the requirement; seeing text appear is
  not a substitute.
- Submitting a question by keyboard alone works end to end.

### V14 — No scraped content and no secrets in version control

**Verifies** FR-032, FR-042, SC-015.

```bash
git grep -nEi 'GROQ_API_KEY=|PINECONE_API_KEY=|sk-[A-Za-z0-9]{16}|gsk_[A-Za-z0-9]{16}'
git ls-files | xargs -I{} sh -c 'test -f "{}" && case "$(file -b --mime-type "{}")" in text/html) echo "{}";; esac'
```

**Expected**: no output from either command. No credential, and no `.html`/`.htm`/`.md` file
containing publisher prose. The committed corpus is references only:
`data/source_manifest.json` (URLs) and `evaluation/golden_questions.json` (questions) — both
constrained by schemas in [contracts/](./contracts/) that have **no field in which page
content could be recorded**.

---

## 5. The reviewer walkthrough (SC-020)

A reviewer must reach the full demonstration in under 15 minutes, following only published
instructions. Verify this end to end before publishing:

| # | Step | Time | Verify |
|---|---|---|---|
| 1 | `docker compose up --build` | 3 min | `/health` reports `ok` on all three dependencies |
| 2 | Open the dashboard, see the source registry | 1 min | SC-010, User Story 7 |
| 3 | Register one source, watch the crawl | 2 min | Live progress; `skipped_reasons` populated |
| 4 | Browse indexed documents, open one to its original page | 1 min | SC-010, SC-018 |
| 5 | Ask a simple question, inspect its evidence and citations | 2 min | SC-018, User Stories 1, 5 |
| 6 | Ask a cross-product question | 1 min | SC-004, User Story 4 |
| 7 | Ask an unanswerable question, confirm the refusal | 1 min | SC-008, User Story 2 |
| 8 | Enable inspection mode, inspect the trace | 1 min | FR-033, FR-034, User Story 5 |
| 9 | Open the evaluation view before any run — empty state | 0.5 min | FR-037, User Story 6 |
| 10 | Run the evaluation, read the metrics and gate outcome | 2 min | SC-001…SC-009 |

If step 9 shows numbers before a run has happened, FR-037 is violated and nothing else in
the demonstration can be trusted.

---

## 6. Troubleshooting

| Symptom | Likely cause | Action |
|---|---|---|
| No events until the answer completes | A proxy or compressor is buffering | Check `Cache-Control: no-cache, no-transform` and `X-Accel-Buffering: no` survive to the client; verify with `curl -N`; if a proxy is in front, disable its compression for this route (R-011) |
| Multi-byte characters garbled mid-answer | The client decoded without `stream: true` | Fix the reader (R-011) |
| Search quality poor, no errors anywhere | `input_type` is backwards on the index | It is fixed at creation — `read_parameters: query`, `write_parameters: passage`. Pinecone's own docs note this degrades quality silently (R-002). Requires index recreation |
| Embeddings look truncated, chunks end mid-sentence | Embedding model max sequence length exceeded | Check `PINECONE_EMBED_MODEL` against the chunk size. `multilingual-e5-large` caps at 512 tokens and would silently truncate 600–1000 token units (R-001) |
| `401` from the language model provider | Deprecated or Enterprise-only model id | `llama-3.3-70b-versatile` went Enterprise-only on 2026-08-16. Use `openai/gpt-oss-120b` (R-006) |
| Answer cites a source that does not exist | Citation validation is not running, or is trusting model output | `source_url` must be looked up from the registry, never taken from the model (FR-003, FR-004). Check `citation_validation` event counts |
| Reasoning text visible in a response | Provider reasoning exposure left at its default | Set `include_reasoning` off and `reasoning_format: hidden` explicitly; verify with the V7 grep (R-014) |
| Structured output rejected with a schema error | A property is missing from `required`, or `additionalProperties` is not `false` | Both are mandatory for strict schemas; express optional fields as nullable types (R-007) |
| `CRAWL_ALREADY_RUNNING` but nothing is running | A job was left in `running` by a crash | Job rows are the record of truth; a stale `running` row will block new crawls. Reconcile it |
| Evaluation metrics identical across different settings | Metrics are not computed from the run | Run V12. This is a Principle VI failure, not a display bug |
| Crawl refuses a legitimate public URL | Over-broad SSRF rule | Check `ALLOWED_DOMAINS`. The guard must refuse internal targets without also refusing the public documentation host (FR-044) |
