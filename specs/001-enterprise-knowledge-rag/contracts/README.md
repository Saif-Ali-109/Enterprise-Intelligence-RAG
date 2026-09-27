# Interface Contracts

Design-time contracts for the Enterprise Knowledge Intelligence RAG system. These are
written during planning; the implementation must conform to them, and the API reference
published with the repository is generated from or kept consistent with them.

| File | Contract | Format | Why this format |
|---|---|---|---|
| [openapi.yaml](./openapi.yaml) | HTTP API surface | OpenAPI 3.1 | The backend is a REST service consumed by a web client, an operator, and the evaluation runner |
| [events.md](./events.md) | Progress event stream | Prose + JSON Schema fragments | The stream is SSE over POST, which OpenAPI cannot describe expressively; a single normative event table is clearer than a fragmented schema |
| [evaluation-dataset.schema.json](./evaluation-dataset.schema.json) | Curated question set | JSON Schema 2020-12 | The dataset is a committed file; a schema makes it machine-validated and structurally prevents the URL pinning that FR-063 forbids |
| [source-manifest.schema.json](./source-manifest.schema.json) | Seed source list | JSON Schema 2020-12 | Same reasoning; `additionalProperties: false` enforces FR-032 at the file level |

---

## Conventions

### Base path and versioning

All backend routes are under `/api/v1`. The frontend exposes a thin BFF at `/api/chat/stream`
that proxies the backend stream; it is transport, not a second contract. See
[events.md](./events.md) §5.

### Identifiers

Opaque UUIDv4 strings. Clients MUST treat them as opaque and MUST NOT parse them. The one
exception is documented: the composite `vector_id` shape is internal to the ingestion
pipeline and is not part of the public contract.

### Timestamps

RFC 3339 / ISO 8601 with explicit offset, UTC: `2026-09-27T14:03:11.482Z`.

### Money, scores, and thresholds

Scores are `REAL` in `[0, 1]`. Similarity and rerank scores from different models are **not
comparable**; the API never presents them on a shared scale or averages them together
(FR-021 scores them as separate signals).

### Error envelope

Every non-2xx response from every endpoint uses this shape. No endpoint returns a bare
`{"detail": ...}` (FR-047).

```json
{
  "error": {
    "code": "SSRF_BLOCKED",
    "message": "Target address is not an allowed public web address.",
    "request_id": "0f3c1d2a-6b4e-4c1f-9a77-2b8e5d0c4a11",
    "details": { "field": "start_url" }
  }
}
```

- `code` — stable, machine-readable, enumerated below. Stable across releases; clients may
  branch on it.
- `message` — safe for display. Never contains a stack trace, internal path, SQL fragment,
  provider error body, or credential.
- `request_id` — always present, including on 5xx. Matches `query_logs.id` when a question
  was being answered, and is the value a user quotes in a bug report.
- `details` — optional, and restricted to field names and enumerated values. Never echoes
  free-form user input back unescaped.

### Error codes

| Code | Status | Meaning | Requirement |
|---|---|---|---|
| `VALIDATION_ERROR` | 400 | Input failed validation or exceeded a bound | FR-043 |
| `SSRF_BLOCKED` | 400 | Target is not an allowed public web address; refused **before** any connection | FR-044, SC-014 |
| `INVALID_URL` | 400 | Not a parseable absolute `http`/`https` URL | FR-044 |
| `ROBOTS_DISALLOWED` | 409 | The starting URL itself is disallowed by the site's directives | FR-028 |
| `UNAUTHORIZED` | 401 | Absent or unusable service credential at startup | FR-042 |
| `NOT_FOUND` | 404 | No such resource | — |
| `CONFLICT` | 409 | Resource state forbids the operation | — |
| `CRAWL_ALREADY_RUNNING` | 409 | A crawl for this source is already in progress; message names the job | FR-055 |
| `DOCUMENT_DELETED` | 409 | The document is tombstoned and cannot be re-crawled | FR-056 |
| `RATE_LIMITED` | 429 | Inbound rate limit exceeded; `Retry-After` set | FR-045 |
| `PROVIDER_ERROR` | 502 | Language model provider failed or returned unusable output | — |
| `PROVIDER_RATE_LIMITED` | 503 | Upstream provider throttled; `Retry-After` set when known | R-008 |
| `VECTOR_SERVICE_UNAVAILABLE` | 503 | Embedding or vector search unreachable. **No answer is produced and no weaker retrieval is substituted** | FR-061, FR-062 |
| `INTERNAL_ERROR` | 500 | Unexpected fault; details withheld, `request_id` returned | FR-047 |

`VECTOR_SERVICE_UNAVAILABLE` is deliberately terminal. The response body contains no partial
results, no cached answer, and no keyword-matched fallback. A degraded answer that looks
complete is worse than a visible failure.

### Idempotency and concurrency

- `POST /sources` is idempotent on `start_url`: re-registering an existing source returns
  `200` with the existing resource, not `409`.
- `POST /sources/{id}/crawl` is **not** idempotent and is guarded by the partial unique
  index on active jobs; a concurrent second request receives `CRAWL_ALREADY_RUNNING`.
- Deletion is idempotent: deleting an already-`deleted` document returns `204`.
- Crawl and evaluation runs are asynchronous. Start endpoints return `202` with a job
  resource; there is no synchronous long-running request.

### Rate limits

Per client address, token bucket, values from configuration. Defaults: **60** requests/minute
for general endpoints, **20** requests/minute for question submission. A single-operator tool
does not need aggressive limits; the requirement is that the bound exists and is applied
(FR-045), not that it is restrictive. `429` carries `Retry-After`.

### Pagination

Offset-based, `limit` (default 50, max 200) and `offset` (default 0). Collections return
`{ "items": [...], "total": <int>, "limit": <int>, "offset": <int> }`. The corpus is bounded at
50–100 pages, so cursor pagination would add complexity for no benefit (Principle XI).
