# API reference

**`specs/001-enterprise-knowledge-rag/contracts/openapi.yaml` is normative.** This document is
generated from it and must never be edited by hand — a hand edit is a contract change made in the
wrong place, where no test can catch it.

Regenerate after any contract change:

```bash
make api-docs      # writes docs/API.md
```

And check it is current (this runs in `make check`):

```bash
make api-docs-check
```

---

## 1. Conventions

**Base path** `/api/v1`, configured by `API_PREFIX`.

**Content type** `application/json` in both directions, except the stream (below).

**Authentication** none. This system has no accounts, no sessions, and no identity; the `401`
code exists in the vocabulary and is not issued by any endpoint here.

**Rate limits** 60 requests/minute general, 20/minute on the ask endpoints. Exceeding either returns
`429` with `Retry-After`.

**Request id** every response carries `X-Response-Request-Id`, and the same value appears in the
error envelope, the server log lines, and the `query_logs` row. Quote it and a failure is traceable.

### Error envelope

Every error, from every endpoint, in one shape:

```json
{
  "error": {
    "code": "VECTOR_SERVICE_UNAVAILABLE",
    "message": "The vector service is unreachable.",
    "request_id": "0f8d2a1c-9b44-4c7d-8e55-6d3a1b2c4e90",
    "details": null
  }
}
```

`message` is safe to show a user. It never contains a stack trace, an internal path, or a credential.
`details` carries structured context where a caller needs it and `null` otherwise.

### Error codes

Fourteen stable codes. A client may branch on these across releases.

| Code | Status | Raised when |
|---|---|---|
| `VALIDATION_ERROR` | 400 | A body, query, or path failed validation |
| `SSRF_BLOCKED` | 400 | An ingestion URL resolves to a refused target |
| `INVALID_URL` | 400 | A URL is syntactically unusable |
| `ROBOTS_DISALLOWED` | 409 | `robots.txt` forbids the path |
| `UNAUTHORIZED` | 401 | Reserved; not issued by any endpoint in this system |
| `NOT_FOUND` | 404 | No such resource |
| `CONFLICT` | 409 | The resource's state forbids the operation (e.g. a second evaluation run) |
| `CRAWL_ALREADY_RUNNING` | 409 | A crawl for this source is in progress |
| `DOCUMENT_DELETED` | 409 | The document has been tombstoned |
| `RATE_LIMITED` | 429 | Too many requests |
| `PROVIDER_ERROR` | 502 | The language model failed or returned unusable output |
| `PROVIDER_RATE_LIMITED` | 503 | The provider's quota is exhausted |
| `VECTOR_SERVICE_UNAVAILABLE` | 503 | The vector store is unreachable |
| `INTERNAL_ERROR` | 500 | An unhandled fault |

---

## 2. System

### `GET /health`

Service state and per-dependency readiness.

```bash
curl -s localhost:8000/api/v1/health
```

```json
{
  "status": "ok",
  "version": "1.0.0",
  "dependencies": {
    "database":       {"status": "ok", "detail": null, "latency_ms": 15},
    "vector_store":   {"status": "ok", "detail": null, "latency_ms": 1094},
    "language_model": {"status": "ok", "detail": null, "latency_ms": 513}
  }
}
```

Each dependency reports independently, so a vector outage reads as a vector outage rather than a
generic failure. `status` is `ok`, `degraded`, or `unavailable`.

### `GET /config`

Effective configuration, live corpus counts, and secret **presence**.

```json
{
  "retrieval": {"candidate_pool": 12, "rerank_top_n": 6, "evidence_min": 3,
                "evidence_max": 6, "filters_enabled": true},
  "generation": {"provider": "groq", "model": "openai/gpt-oss-120b",
                 "classification_model": "openai/gpt-oss-20b",
                 "max_attempts": 2, "reasoning_exposed": false},
  "index": {"name": "enterprise-knowledge", "namespace": "atlassian-public",
            "embed_model": "llama-text-embed-v2", "dimension_source": "service",
            "rerank_model": "bge-reranker-v2-m3"},
  "corpus": {"source_count": 1, "document_count": 19, "unit_count": 149,
             "allowed_domains": ["support.atlassian.com"]},
  "secrets": {"pinecone_api_key": {"configured": true},
              "groq_api_key": {"configured": true},
              "database_url": {"configured": true}}
}
```

`secrets` entries carry **no value field at all** — `SecretPresence` is `{configured: boolean}` and
the schema has nowhere to put a value. `corpus` is counted from live rows, not read from
configuration, so it reflects what is searchable rather than what was configured.

`min_rerank_score` and `min_evidence_score` are **omitted** when unset, which is the default. Their
absence is deliberate: an invented threshold would make the quality gate fire at an arbitrary point.

---

## 3. Ask

### `POST /chat`

```bash
curl -s -X POST localhost:8000/api/v1/chat \
  -H 'Content-Type: application/json' \
  -d '{"question": "How do I create a JQL filter?"}'
```

Request: `question` (1–2000 chars, required), `inspect` (bool, default `false`), `history`
(up to 10 prior turns).

Response — answered:

```json
{
  "request_id": "…", "question": "…", "outcome": "answered",
  "answer": "Open the issue type screen… [1]",
  "citations": [
    {"rank": 1, "document_id": "…", "unit_id": "…",
     "source_url": "https://support.atlassian.com/jira-cloud-administration/docs/…",
     "title": "Filter manager", "product": "jira", "category": "filters",
     "heading_path": ["Filters", "Saving a filter"],
     "quote": "A saved filter can be reused…",
     "retrieval_score": 0.83, "rerank_score": 0.91, "validation_state": "valid"}
  ],
  "leads": [], "refusal_reason": null, "searched": null,
  "query_analysis": {"detected_product": "jira", "…": "…"},
  "timing": {"total_ms": 1810, "stages_ms": {"retrieve": 210, "rerank": 180}}
}
```

Response — refused (HTTP 200; a refusal is a result, not an error):

```json
{
  "request_id": "…", "outcome": "refused",
  "refusal_reason": "INSUFFICIENT_EVIDENCE",
  "answer": null, "citations": [],
  "searched": {"queries": ["…"], "products": ["jira"],
               "applied_filters": {"product": "jira", "category": "filters"},
               "candidates_retrieved": 12, "candidates_reranked": 6,
               "evidence_selected": 0},
  "leads": [{"source_url": "…", "title": "…", "heading_path": [],
             "relevance": 0.0, "insufficient": true}]
}
```

`refusal_reason` is one of `INSUFFICIENT_EVIDENCE`, `UNSUPPORTED_INTENT`, `TOO_AMBIGUOUS`,
`GENERATION_FAILED`.

**`leads` are the partial answer.** When the evidence is too thin to answer but not worthless, the
refusal carries the best passages found with `insufficient: true`. A user gets a door, not a shrug.

**`inspect: true`** adds `trace` with the four request-scoped artifacts — analysed question,
retrieved candidates, reranked candidates, selected evidence — plus per-citation validation counts.
It contains pipeline state and counts, never a reasoning field, because no such field exists.

**`GET /chat/{request_id}`** returns `404`. Answers are not persisted for retrieval; the audit record
is for operators, not for clients.

### `POST /chat/stream`

The same pipeline, streamed. `Accept: text/event-stream`.

```bash
curl -sN -X POST localhost:8000/api/v1/chat/stream \
  -H 'Content-Type: application/json' -H 'Accept: text/event-stream' \
  -d '{"question": "How do I create a JQL filter?"}'
```

Eight frames in this order, then `data: [DONE]`:

```
event: query_received          {request_id, question, inspect}
event: query_analyzed          {request_id, detected_product, detected_category, intent,
                                entities, classification_confidence, rewrite_queries,
                                applied_filters, filters_suppressed, ambiguity_note}
event: retrieval_started       {request_id, candidate_pool, query_count}
event: retrieval_completed     {request_id, candidates_retrieved, queries_executed[]}
event: reranking_completed     {request_id, candidates_reranked, rerank_model}
event: generation_started      {request_id, evidence_count, attempt}
event: citation_validation     {request_id, citations_valid, citations_stripped,
                                citations_repaired, rejected_identifiers[]}
event: answer_completed        {request_id, outcome, answer, citations[], total_ms, …}
```

Exactly one terminal frame — `answer_completed` or `error`, never both and never neither. A stage
that fails is reported by `error`, not by a zero-count success frame.

Frames 1–7 are never skipped on a run that proceeds: a stage that produced nothing still reports
with a zero count, so a client never waits forever for a frame that will not come. Events 4–7 carry
zero counts when the run stops early and the terminal frame arrives instead.

The normative specification is
[`contracts/events.md`](../specs/001-enterprise-knowledge-rag/contracts/events.md), including which
events are announced to assistive technology.

---

## 4. Sources and crawling

| Endpoint | Purpose |
|---|---|
| `GET /sources` | Registered sources with live page and unit counts |
| `POST /sources` | Register a source and start its first crawl → `201` with the source and its `crawl_job` |
| `GET /sources/{id}` | One source |
| `PATCH /sources/{id}` | Update name, enabled flag, product domain, crawl bounds |
| `DELETE /sources/{id}` | Stop indexing a source and remove its pages from the index |
| `GET /crawl-jobs` | Crawl history, filterable by source and status |
| `GET /crawl-jobs/{id}` | One job with its five counters, skip reasons, and error detail |

```bash
curl -s -X POST localhost:8000/api/v1/sources \
  -H 'Content-Type: application/json' \
  -d '{"name": "Atlassian support knowledge base",
       "start_url": "https://support.atlassian.com/",
       "product_domain": "jira",
       "max_pages": 25, "max_depth": 2, "delay_seconds": 0.4}'
```

**Registration is idempotent by `start_url`.** A repeated registration returns `200` with the
existing source; a new one returns `201`. The `201` body carries `crawl_job` (nullable); the `200`
body is the bare source.

**A second crawl of a source in flight returns `409 CRAWL_ALREADY_RUNNING`** and names the job id in
`details`. `POST /sources/{id}/crawl` starts one on demand → `202`.

Crawl job counters are five, and they answer *why did this index fewer pages than it discovered*:
`discovered`, `processed`, `unchanged`, `skipped`, `failed`. `skipped_reasons` is a flat
reason-to-count map — `{"robots_disallowed": 3}` — and is `null` when nothing was skipped, so "nothing
was skipped" is distinguishable from "this job never reported a breakdown".

---

## 5. Documents

| Endpoint | Purpose |
|---|---|
| `GET /documents` | Browse and filter: `source_id`, `product`, `category`, `page_type`, `state`, `q` |
| `GET /documents/{id}` | One document with provenance, fingerprint, and lifecycle state |
| `DELETE /documents/{id}` | Remove a document from the registry and the vector index |
| `GET /documents/{id}/units` | Its units in ordinal order, **without their text** |

Tombstoned documents stay browsable with their `state` visible. A tombstone is evidence — a page
that disappeared upstream — not clutter to hide.

---

## 6. Evaluation

| Endpoint | Purpose |
|---|---|
| `GET /evaluations/dataset` | The curated question set. No URL field exists anywhere in it |
| `GET /evaluations/runs` | Run history, newest first. Empty collection when no run exists |
| `POST /evaluations/runs` | Start a run → `202`. `409` while one is in progress |
| `GET /evaluations/runs/{id}` | One run: `metrics`, `thresholds`, `gate_outcome`, `gate_failures` |
| `GET /evaluations/runs/{id}/results` | Per-question rows behind the aggregates |

`gate_outcome` is strictly `pass` or `fail`. `metrics` is `null` until the run finishes — never
zeroes, never estimates. See [EVALUATION.md](EVALUATION.md) for the measured results and how to read
a failing gate.

---

## 7. Pagination

List endpoints take `limit` (1–200, default 50) and `offset` (≥ 0) and return:

```json
{"items": [...], "total": 41, "limit": 25, "offset": 0}
```

`total` is counted with the same filter as the rows, so a client never sees an empty final page while
the count still claims there are more.

---

<!-- BEGIN GENERATED: contracts/openapi.yaml -->

Contract `specs/001-enterprise-knowledge-rag/contracts/openapi.yaml` · 1.0.0

### `chat`

| Method | Path | Summary |
|---|---|---|
| `POST` | `/chat` | Ask a question and receive a complete grounded answer |
| `POST` | `/chat/stream` | Ask a question and receive pipeline progress as Server-Sent Events |
| `GET` | `/chat/{request_id}` | Replays a stored answer. |

### `crawl-jobs`

| Method | Path | Summary |
|---|---|---|
| `GET` | `/crawl-jobs` | List crawl jobs |
| `GET` | `/crawl-jobs/{job_id}` | Retrieve a crawl job |

### `documents`

| Method | Path | Summary |
|---|---|---|
| `GET` | `/documents` | Browse indexed documents |
| `DELETE` | `/documents/{document_id}` | Tombstones the document, then removes its vectors, so the content is unreachable in subsequent answers (FR-031, FR-056). |
| `GET` | `/documents/{document_id}` | Retrieve a document |
| `GET` | `/documents/{document_id}/units` | Returns provenance for each unit — heading path, ordinals, token count, block types — but not the unit text, which is held in the vector store. |

### `evaluations`

| Method | Path | Summary |
|---|---|---|
| `GET` | `/evaluations/dataset` | Retrieve the curated question set |
| `GET` | `/evaluations/runs` | Returns an empty collection when no run exists. |
| `POST` | `/evaluations/runs` | Asynchronous — a 30-plus question run makes a synchronous request untenable. |
| `GET` | `/evaluations/runs/{run_id}` | Retrieve an evaluation run with its metrics and gate outcome |
| `GET` | `/evaluations/runs/{run_id}/results` | The per-question detail behind the aggregate metrics, including the `fabricated_fact_count` and `invalid_citation_count` figures that the absolute gates depend on. |

### `sources`

| Method | Path | Summary |
|---|---|---|
| `GET` | `/sources` | List registered sources |
| `POST` | `/sources` | The URL is validated against the address guard **before any connection is opened** (FR-044). |
| `DELETE` | `/sources/{source_id}` | Removes the registry rows **and** the indexed vectors, so the content can no longer be retrieved (FR-031). |
| `GET` | `/sources/{source_id}` | Retrieve a source |
| `PATCH` | `/sources/{source_id}` | Update a source, including enabling or disabling it |
| `POST` | `/sources/{source_id}/crawl` | Request a re-crawl of a source |

### `system`

| Method | Path | Summary |
|---|---|---|
| `GET` | `/config` | Returns the settings that affect retrieval and generation so an operator can see why the system behaves as it does. |
| `GET` | `/health` | Liveness and dependency readiness |

### Response codes used by this contract

| Status | Where |
|---|---|
| `POST /chat` | `200`, `400`, `429`, `502`, `503` |
| `POST /chat/stream` | `200`, `400`, `429`, `503` |
| `GET /chat/{request_id}` | `200`, `404` |
| `GET /config` | `200` |
| `GET /crawl-jobs` | `200` |
| `GET /crawl-jobs/{job_id}` | `200`, `404` |
| `GET /documents` | `200` |
| `GET /documents/{document_id}` | `200`, `404` |
| `DELETE /documents/{document_id}` | `204`, `404` |
| `GET /documents/{document_id}/units` | `200`, `404` |
| `GET /evaluations/dataset` | `200` |
| `GET /evaluations/runs` | `200` |
| `POST /evaluations/runs` | `202`, `409`, `503` |
| `GET /evaluations/runs/{run_id}` | `200`, `404` |
| `GET /evaluations/runs/{run_id}/results` | `200`, `404` |
| `GET /health` | `200` |
| `GET /sources` | `200` |
| `POST /sources` | `200`, `201`, `400`, `409` |
| `GET /sources/{source_id}` | `200`, `404` |
| `PATCH /sources/{source_id}` | `200`, `404` |
| `DELETE /sources/{source_id}` | `204`, `404` |
| `POST /sources/{source_id}/crawl` | `202`, `404`, `409` |

### Schemas

| Schema | Required fields |
|---|---|
| `AskRequest` | `question` |
| `AskResponse` | `request_id`, `outcome`, `citations`, `timing` |
| `Citation` | `rank`, `document_id`, `unit_id`, `source_url`, `title`, `validation_state` |
| `CrawlJob` | `id`, `source_id`, `target_url`, `status`, `requested_at`, `counters` |
| `CrawlJobStatus` | — |
| `DependencyStatus` | `status` |
| `Document` | `id`, `source_id`, `url`, `state`, `vectors_live`, `unit_count` |
| `DocumentState` | — |
| `DocumentUnit` | `id`, `document_id`, `ordinal`, `heading_path`, `token_count`, `block_types` |
| `Error` | `error` |
| `EvaluationDataset` | `dataset_version`, `questions` |
| `EvaluationQuestion` | `id`, `question`, `category`, `difficulty`, `is_unsupported`, `expected` |
| `EvaluationResult` | `question_id`, `refused`, `fabricated_fact_count`, `invalid_citation_count` |
| `EvaluationRun` | `id`, `dataset_version`, `status`, `gate_outcome` |
| `Lead` | `source_url`, `title`, `insufficient` |
| `PipelineTrace` | `stages` |
| `QueryAnalysis` | `original`, `confidence`, `rewrite_queries` |
| `RerankedCandidate` | `unit_id`, `rerank_score`, `rank` |
| `RetrievedCandidate` | `unit_id`, `document_id`, `similarity_score` |
| `SearchedScope` | `queries`, `candidates_retrieved` |
| `SecretPresence` | `configured` |
| `SelectedEvidence` | `unit_id`, `rank` |
| `Source` | `id`, `name`, `start_url`, `allowed_domains`, `enabled`, `status`, `page_count`, `unit_count` |
| `SourceCreate` | `name`, `start_url` |
| `SourceUpdate` | — |

<!-- END GENERATED -->

## 8. Conventions this API keeps

- **`null` and absent mean different things.** `null` is "this does not apply"; an absent field is
  "not part of this response shape". A `0` always means a measurement of zero.
- **A refusal is `200`.** It is a result, with a reason and possibly `leads`, not an error.
- **A degraded dependency is visible as itself.** `503 VECTOR_SERVICE_UNAVAILABLE` names the vector
  store; there is no silent fallback path that produces a weaker answer instead.
- **No field anywhere carries a secret, a score that is not a measurement, or a reasoning channel.**
  This is asserted by tests, not by convention.