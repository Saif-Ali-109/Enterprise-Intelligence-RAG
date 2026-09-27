# Phase 1 Data Model: Enterprise Knowledge Intelligence RAG

**Date**: 2026-09-27 | **Spec**: [spec.md](./spec.md) | **Plan**: [plan.md](./plan.md) | **Research**: [research.md](./research.md)

Derived from the twelve Key Entities in the spec. Entity names and attributes are the
spec's; this document adds types, constraints, indexes, and lifecycle rules, and records
which entities are persisted relationally and which are request-scoped.

---

## 1. Persistence decisions

### 1.1 What is relational, and what is not

Nine of the twelve entities become tables. Four are **request-scoped pipeline artifacts** that
are never queried relationally — they are read as one whole, per question.

| Spec entity | Persisted as | Rationale |
|---|---|---|
| Source | table `sources` | Registry of record (FR-030) |
| Document | table `documents` | Registry of record, lifecycle (FR-024, FR-053) |
| Document Unit | table `document_units` | Traceability (FR-018) |
| Crawl Job | table `crawl_jobs` | Failure attribution (FR-054) |
| Query Log | table `query_logs` | Audit (FR-048) |
| Citation | table `citations` | Validated references (FR-004) |
| Evaluation Question | table `evaluation_questions` | Curated set (FR-039) |
| Evaluation Run | table `evaluation_runs` | Run identity (FR-036) |
| *(per-question run detail)* | table `evaluation_results` | Audit of the zero-tolerance gates |
| Question Analysis | in-memory value object | No independent query exists |
| Retrieved Candidate | in-memory value object | Same |
| Reranked Candidate | in-memory value object | Same |
| Selected Evidence | in-memory value object | Same |

The four pipeline artifacts are snapshotted as a single JSON document on
`query_logs.pipeline_trace` when inspection mode is on, so a past answer remains
inspectable. Promoting any of them to a table requires a stated relational query that a
JSON document cannot serve; none exists today.

### 1.2 Deviation from the original brief's six-table sketch

The brief sketched six tables. Nine are defined here. The three additions are not
cosmetic:

| Addition | Why the brief's six could not satisfy it |
|---|---|
| `evaluation_results` | SC-008 and SC-009 are **absolute zeros** — zero fabricated facts, zero invalid citation identifiers. An aggregate pass/fail cannot be audited or debugged; the count must be traceable to an individual question |
| `crawl_jobs` separated from `sources` | FR-054 requires recording a failure **against the affected crawl job** while the document's prior content stays live. Job history must outlive the current run |
| `vectors_live` + tombstone columns on `documents` | FR-056 requires deletion to win over an in-flight crawl, and FR-054 requires vectors to survive a failed re-crawl. Neither is expressible without distinguishing *why* a document has no live content |

---

## 2. Entity definitions

Types are PostgreSQL. `*_at` columns are `TIMESTAMPTZ`. Enumerations are implemented as
`CHECK`-constrained `TEXT` (or native enum types) — the exact choice is a migration
concern, the permitted values are a contract.

### 2.1 `sources`

A registered public documentation entry point.

| Column | Type | Null | Rules |
|---|---|---|---|
| `id` | `UUID` | no | PK |
| `name` | `TEXT` | no | non-empty |
| `start_url` | `TEXT` | no | unique; must pass the address guard (FR-044) |
| `allowed_domains` | `TEXT[]` | no | non-empty; crawl may not leave this set |
| `product_domain` | `TEXT` | **yes** | `null` = cross-product source (FR-013) |
| `enabled` | `BOOLEAN` | no | default `true` |
| `status` | `TEXT` | no | `active` \| `disabled` \| `error` |
| `last_crawl_at` | `TIMESTAMPTZ` | yes | |
| `created_at` / `updated_at` | `TIMESTAMPTZ` | no | |

**Validation**: `start_url` scheme ∈ {`http`, `https`} only. `allowed_domains` derives from
the registrant's entry, never from a client-supplied string that bypasses the guard.

**Counts are derived, not stored.** The spec's Source entity lists "page and chunk counts".
Persisting them invites drift against `documents` and `document_units`, which are the
authority. They are computed on read (or maintained by the same transaction that writes the
child rows). `page_count` = `COUNT(documents WHERE state != 'deleted')`;
`chunk_count` = `COUNT(document_units)` joined to live documents.

### 2.2 `documents`

One ingested page. This table carries the lifecycle (FR-053) and the provenance
(FR-024).

| Column | Type | Null | Rules |
|---|---|---|---|
| `id` | `UUID` | no | PK |
| `source_id` | `UUID` | no | FK → `sources`, `ON DELETE CASCADE` |
| `url` | `TEXT` | no | unique per source |
| `canonical_url` | `TEXT` | yes | used for deduplication when detectable |
| `title` | `TEXT` | yes | |
| `product` | `TEXT` | yes | `null` permitted — FR-009 forbids forcing a classification |
| `category` | `TEXT` | yes | `null` permitted, same reason |
| `page_type` | `TEXT` | yes | |
| `language` | `TEXT` | yes | |
| `content_fingerprint` | `TEXT` | yes | SHA-256 of normalised main content |
| `source_modified_at` | `TIMESTAMPTZ` | yes | "version information where detectable" (FR-024) |
| `last_crawled_at` | `TIMESTAMPTZ` | yes | |
| `unit_count` | `INTEGER` | no | derived; mirrors `document_units` for list views |
| `state` | `TEXT` | no | `discovered` \| `crawling` \| `processed` \| `indexed` \| `failed` \| `deleted` |
| `state_detail` | `JSONB` | yes | error code and safe message when `failed` |
| `vectors_live` | `BOOLEAN` | no | default `false`; see §3.2 |
| `indexed_at` | `TIMESTAMPTZ` | yes | when vectors were last written |
| `tombstoned_at` | `TIMESTAMPTZ` | yes | when deletion was requested (FR-056) |
| `created_at` / `updated_at` | `TIMESTAMPTZ` | no | |

**Constraints**:
- `UNIQUE (source_id, url)`.
- `tombstoned_at IS NOT NULL` **implies** `state = 'deleted'` — a tombstone without the
  terminal state would leave content queryable, defeating FR-056.
- `content_fingerprint` is **not** unique. Two pages may legitimately share content
  (FR edge case: duplicate detection). Deduplication is per source and is a crawler
  decision recorded on the job, not a database constraint — otherwise a shared boilerplate
  page would block a legitimate second document.

### 2.3 `document_units`

A retrievable, structure-derived segment (FR-015, FR-018).

| Column | Type | Null | Rules |
|---|---|---|---|
| `id` | `UUID` | no | PK |
| `document_id` | `UUID` | no | FK → `documents`, `ON DELETE CASCADE` |
| `ordinal` | `INTEGER` | no | position within the document |
| `vector_id` | `TEXT` | no | unique; `{document_id}#{ordinal zero-padded 4}` (R-005) |
| `text_fingerprint` | `TEXT` | no | SHA-256 of unit text |
| `heading_path` | `TEXT[]` | no | may be empty; **not** `null` (FR-018) |
| `block_types` | `TEXT[]` | no | dominant types present, e.g. `{paragraph, code}` |
| `token_count` | `INTEGER` | no | > 0 |
| `overlap_tokens` | `INTEGER` | no | deliberate overlap with the previous unit (FR-016) |
| `created_at` | `TIMESTAMPTZ` | no | |

**Constraints**: `UNIQUE (document_id, ordinal)`; `UNIQUE (vector_id)`.
`vector_id` is the join key to the vector store and is **immutable** — regenerating it would
orphan live vectors.

Unit **text is not stored here.** It lives in the vector store as the embedded field. The
registry holds provenance and identity; duplicating the text would create two copies of
third-party content to keep in sync and would put scraped content in the relational store
for no retrieval benefit. Rationale for holding the *excerpt* used in an answer is in §2.6.

### 2.4 `crawl_jobs`

One crawl run against a source (FR-025, FR-028, FR-054).

| Column | Type | Null | Rules |
|---|---|---|---|
| `id` | `UUID` | no | PK; returned to the operator as the job handle |
| `source_id` | `UUID` | no | FK → `sources`, `ON DELETE CASCADE` |
| `target_url` | `TEXT` | no | the registered starting point |
| `status` | `TEXT` | no | `queued` \| `running` \| `completed` \| `completed_with_errors` \| `failed` \| `cancelled` |
| `requested_at` | `TIMESTAMPTZ` | no | |
| `started_at` / `finished_at` | `TIMESTAMPTZ` | yes | |
| `pages_discovered` | `INTEGER` | no | default 0 |
| `pages_processed` | `INTEGER` | no | default 0 |
| `pages_unchanged` | `INTEGER` | no | default 0 — SC-012 evidence |
| `pages_skipped` | `INTEGER` | no | default 0 — includes robots-disallowed |
| `pages_failed` | `INTEGER` | no | default 0 |
| `skipped_reasons` | `JSONB` | yes | `{robots_disallowed: 3, too_boilerplate: 1, duplicate: 2}` |
| `error_code` | `TEXT` | yes | safe code, never a stack trace (FR-047) |
| `error_message` | `TEXT` | yes | safe, human-readable |

**FR-055 is enforced by a partial unique index**, not application logic alone:

```sql
CREATE UNIQUE INDEX crawl_jobs_one_active_per_source
    ON crawl_jobs (source_id)
    WHERE status IN ('queued', 'running');
```

A second crawl request violates the index and is rejected with a message naming the
in-flight job. This makes the "one active crawl per source" rule a database guarantee: it
holds under concurrency and cannot be bypassed by a code path that forgets to check.

### 2.5 `query_logs`

Audit record of one answered question (FR-048) and the host of the inspection trace
(FR-033).

| Column | Type | Null | Rules |
|---|---|---|---|
| `id` | `UUID` | no | PK; **is** the `request_id` returned to the client (FR-047) |
| `question` | `TEXT` | no | bounded length (FR-043) |
| `outcome` | `TEXT` | no | `answered` \| `refused` \| `error` |
| `answer` | `TEXT` | yes | `null` on refusal and on error |
| `refusal_reason` | `TEXT` | yes | populated only when `outcome = 'refused'` |
| `detected_product` / `detected_category` / `intent` | `TEXT` | yes | `null` = explicit unknown (FR-009) |
| `classification_confidence` | `JSONB` | yes | per-field confidence |
| `rewrite_queries` | `TEXT[]` | no | default empty |
| `applied_filters` | `JSONB` | no | default `{}` |
| `candidates_retrieved` | `INTEGER` | no | FR-048 requires this count |
| `candidates_reranked` | `INTEGER` | no | |
| `evidence_selected` | `INTEGER` | no | |
| `stage_latency_ms` | `JSONB` | no | per stage |
| `total_latency_ms` | `INTEGER` | no | |
| `provider` / `model` | `TEXT` | yes | FR-048 |
| `token_usage` | `JSONB` | yes | `prompt_tokens`, `completion_tokens`, `total_tokens`, `total_time` (R-008) |
| `rerank_model` | `TEXT` | yes | |
| `pipeline_trace` | `JSONB` | yes | populated only when inspection mode is on |
| `error_code` | `TEXT` | yes | |
| `created_at` | `TIMESTAMPTZ` | no | indexed; drives bounded retention |

**No secret or provider credential column exists** (FR-042). Retention is a purge by
`created_at` against a configured window; the column must be indexed for that to be cheap.

### 2.6 `citations`

A validated evidence reference attached to an answer (FR-002, FR-004, FR-005).

| Column | Type | Null | Rules |
|---|---|---|---|
| `id` | `UUID` | no | PK |
| `query_log_id` | `UUID` | no | FK → `query_logs`, `ON DELETE CASCADE` |
| `rank` | `INTEGER` | no | order of appearance in the answer |
| `document_id` | `UUID` | no | FK → `documents` |
| `unit_id` | `UUID` | no | FK → `document_units` |
| `source_url` | `TEXT` | no | **registry-derived**, not model-supplied (FR-050) |
| `title` / `product` / `category` | `TEXT` | yes | denormalised for citation display |
| `heading_path` | `TEXT[]` | no | |
| `quote` | `TEXT` | no | the supporting span, for display and audit |
| `retrieval_score` / `rerank_score` | `REAL` | yes | |
| `validation_state` | `TEXT` | no | `valid` \| `stripped` \| `repaired` |
| `validation_note` | `TEXT` | yes | why a citation was altered |

**`source_url` is the authority, not the model.** FR-004 requires validation to confirm the
link "matches the registry of record", and FR-003 forbids the model from authoring a source
link. So a citation's link is *looked up* from `documents`, never taken from model output.
The link the model claimed is compared during validation and the outcome recorded in
`validation_state` / `validation_note`; it is not stored as if it were true.

`quote` is the **only** place third-party text is retained outside the vector store. It is
bounded to the supporting span (not a whole unit), lives in the database rather than version
control (FR-032 concerns Git), and exists so a reviewer can check that a cited span actually
supports its claim without re-fetching the page.

### 2.7 `evaluation_questions`

A curated assessment item (FR-039, FR-040, FR-063).

| Column | Type | Null | Rules |
|---|---|---|---|
| `id` | `TEXT` | no | PK; stable, human-readable |
| `question` | `TEXT` | no | |
| `category` | `TEXT` | no | `simple_lookup` \| `multi_part` \| `cross_product` \| `troubleshooting` \| `permissions` \| `api` \| `ambiguous` \| `unsupported` |
| `difficulty` | `TEXT` | no | `easy` \| `medium` \| `hard` |
| `is_unsupported` | `BOOLEAN` | no | the deliberately-unsupported bucket (SC-008) |
| `expected_product` / `expected_category` | `TEXT` | yes | topic expectations |
| `expected_heading_path` | `TEXT[]` | no | may be empty |
| `expected_topics` | `TEXT[]` | no | non-empty; topic coverage, never a canonical answer string |
| `answer_notes` | `TEXT` | yes | accepts multiple valid answers (FR-040) |
| `created_at` | `TIMESTAMPTZ` | no | |

**There is deliberately no `source_url` column** (FR-063, FR-064). Grading is by topic
coverage, so a publisher reorganising their documentation cannot invalidate the dataset. The
same exclusion is enforced structurally in
[contracts/evaluation-dataset.schema.json](./contracts/evaluation-dataset.schema.json), so a
pinned URL cannot be smuggled in through the file either.

### 2.8 `evaluation_runs` and `evaluation_results`

`evaluation_runs` — one execution of the suite (FR-036, FR-041).

| Column | Type | Null | Rules |
|---|---|---|---|
| `id` | `UUID` | no | PK |
| `dataset_version` | `TEXT` | no | dataset identity the run is bound to |
| `dataset_fingerprint` | `TEXT` | no | content hash; a run is never silently comparable across datasets |
| `config_snapshot` | `JSONB` | no | model, embed model, thresholds, retrieval budget used |
| `started_at` / `finished_at` | `TIMESTAMPTZ` | no / yes | |
| `question_count` | `INTEGER` | no | |
| `metrics` | `JSONB` | no | every value computed from this run (FR-036) |
| `thresholds` | `JSONB` | no | the gate values in force for this run |
| `gate_outcome` | `TEXT` | no | `pass` \| `fail` — never a soft "warning" (FR-041) |
| `gate_failures` | `JSONB` | no | `[]` when passing; otherwise which gate, target, actual |

`gate_outcome` is a strict two-valued field by design. SC-001…SC-009 are pass/fail gates;
an outcome that can render as "marginal" would let a failing run read as a soft result.

`evaluation_results` — one row per question per run.

| Column | Type | Null | Rules |
|---|---|---|---|
| `id` | `UUID` | no | PK |
| `run_id` | `UUID` | no | FK → `evaluation_runs`, `ON DELETE CASCADE` |
| `question_id` | `TEXT` | no | FK → `evaluation_questions` |
| `recall_at_5` / `precision_at_5` / `mrr` | `REAL` | yes | `null` for `is_unsupported` questions — retrieval grading is undefined there |
| `citation_validity` | `REAL` | yes | |
| `claim_coverage` | `REAL` | yes | |
| `faithfulness` | `REAL` | yes | |
| `refused` | `BOOLEAN` | no | |
| `fabricated_fact_count` | `INTEGER` | no | **must be 0** (SC-008) |
| `invalid_citation_count` | `INTEGER` | no | **must be 0** (SC-009) |
| `latency_ms` | `INTEGER` | yes | |
| `error` | `TEXT` | yes | |

**Nulls are meaningful.** A `null` metric means *not applicable to this question*, never
*not measured*. An `unsupported` question has no recall to compute; recording `0.0` there
would drag a real average down and misrepresent a refusal as a retrieval failure.

---

## 3. Document lifecycle

### 3.1 State machine (FR-053)

```text
                     ┌──────────────┐
                     │  discovered  │  registered, not fetched
                     └──────┬───────┘
                            │ crawl picks up
                     ┌──────▼───────┐
        ┌───────────►│   crawling   │  fetch in progress
        │            └──────┬───────┘
        │                   │ extracted + chunked; NO vectors yet
        │            ┌──────▼───────┐
        │            │  processed   │
        │            └──────┬───────┘
        │                   │ vectors written; retrievable
        │            ┌──────▼───────┐
        │            │   indexed    │◄──── re-crawl (content stays live throughout)
        │            └──────┬───────┘
        │                   │
        │            ┌──────▼───────┐
        └────────────┤   failed     │  fetch or processing errored
     retry           └──────────────┘

   any state ──operator deletes──► deleted   (terminal, tombstoned)
```

### 3.2 The rule that makes FR-054 and FR-056 work

**Vectors are removed on transition to `deleted`, and on no other transition.**

This is the whole mechanism. Consequences, each traceable to a requirement:

- A re-crawl that fails partway moves `indexed → failed` but leaves `vectors_live = true`.
  The document's prior content stays retrievable (FR-054), and only pages that actually
  changed are superseded.
- `failed` reached from `discovered` or `crawling` has `vectors_live = false` — there was
  never anything to keep.
- `deleted` is terminal and sets `tombstoned_at`. An in-flight crawl checks the tombstone
  before writing units or vectors and skips the document, so content cannot resurrect
  (FR-056). **Deletion always wins.**
- The crawler never transitions a document *out of* `deleted`.

`vectors_live` is therefore not derivable from `state` alone — `failed` means opposite
things depending on the prior state. It is stored explicitly for that reason, and the
transition table that sets it is the single place vectors are written or destroyed.

### 3.3 Supersession (FR-026)

When a re-crawl finds a changed `content_fingerprint`:

1. Fetch and extract the new content **before** touching the live index.
2. Write the new units; write new vectors under the same `vector_id` scheme.
3. Only then mark the document `indexed` with the new `indexed_at`.
4. Delete orphaned vectors — units that no longer exist at the new ordinal boundary.

A changed page is never in a state where its old content is gone and its new content is
not yet indexed. A failed step at any point leaves the previous version live, which is
exactly the mid-crawl-failure behaviour FR-054 mandates. The operational consequence to
accept: a partially-superseded document can briefly serve mixed-version evidence, which the
`content_fingerprint` on each citation makes detectable.

---

## 4. Indexes

| Index | Serves |
|---|---|
| `documents (source_id, url)` unique | Registration idempotency |
| `documents (state)` | Corpus view filtered by state |
| `documents (source_id) WHERE state <> 'deleted'` | Per-source page count |
| `documents (tombstoned_at) WHERE tombstoned_at IS NOT NULL` | Fast in-crawl tombstone check (FR-056) |
| `document_units (document_id, ordinal)` unique | Ordered read |
| `document_units (vector_id)` unique | Join to the vector store (R-005) |
| `crawl_jobs_one_active_per_source` partial unique | **FR-055 database guarantee** |
| `crawl_jobs (source_id, requested_at DESC)` | Job history |
| `query_logs (created_at)` | Bounded retention purge (FR-048) |
| `citations (query_log_id, rank)` | Ordered citation render |
| `evaluation_results (run_id, question_id)` unique | Per-question audit |

The vector store's own `document_id` metadata field serves document-scoped deletion
(R-005, FR-031). It is a filter field, so a deletion is a single scoped call rather than an
enumeration of vector IDs — which is also what makes tombstoning a document mid-crawl safe.

---

## 5. Metadata carried in the vector store

Flat and filterable; the embedded field is the only text sent for embedding (R-003).
`namespace` is reserved and therefore never appears here.

| Field | Type | Used by |
|---|---|---|
| `chunk_text` | text (embedded) | embedding input; **not** filterable |
| `document_id` | keyword | document-scoped delete, supersession (FR-031) |
| `source_id` | keyword | source-scoped queries |
| `product` | keyword | domain filtering (FR-014) |
| `category` | keyword | domain filtering (FR-014) |
| `page_type` | keyword | page-type filtering (FR-014) |
| `language` | keyword | language filtering (FR-014) |
| `heading_path` | text | display and heading-relevance scoring (FR-021) |
| `title` | text | display |
| `source_url` | text | citation link (FR-050) |
| `ordinal` | integer | ordering and supersession |
| `content_fingerprint` | keyword | unchanged-detection evidence (FR-026) |
| `indexed_at` | text | provenance display |

Every field here is retrievable from `documents` / `document_units` by `document_id` +
`ordinal`. It is denormalised into the vector store so a single search call can filter and
render without a second round trip. The registry remains authoritative; the vector store copy
serves the query path only.
