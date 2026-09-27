# Progress Event Stream Contract

Normative contract for the Server-Sent Events stream served by `POST /chat/stream`.

**Why this is a separate document.** The stream is SSE over a `POST` body. OpenAPI cannot
describe it without contortion, and fragmenting it across a dozen schema definitions would
make the one thing that matters most — the guaranteed event order — harder to see than a
single table. The HTTP surface is in [openapi.yaml](./openapi.yaml); the event sequence and
payloads are here.

---

## 1. Transport

| Property | Value |
|---|---|
| Method | `POST` |
| Path | `/api/v1/chat/stream` |
| Request `Content-Type` | `application/json` |
| Request `Accept` | `text/event-stream` |
| Response `Content-Type` | `text/event-stream; charset=utf-8` |
| Response `Cache-Control` | `no-cache, no-transform` |
| Response `X-Accel-Buffering` | `no` |
| Framing | Standard SSE: `event:`, `data:`, blank-line separator |
| Termination | `data: [DONE]` |

`POST` is required, not a preference. `EventSource` issues `GET` only, cannot set headers,
and cannot send a body — so it structurally cannot carry a question. Clients use
`fetch` with a `ReadableStream` reader (R-011).

## 2. Frame format

```text
event: <event-name>
data: <compact JSON>
<blank line>
```

- `data` is **compact JSON** — no newlines inside it. A pretty-printed payload would be
  split across `data:` lines and corrupt the frame.
- Every payload carries `request_id`. It is the correlation key for the whole request and
  matches `query_logs.id`, so a stream can be tied to its audit record after the fact
  (FR-047, FR-048).
- Payloads are closed to additional properties. An unrecognised field is a contract
  violation, not something to forward.
- `id:` and `retry:` are not used. There is no reconnection semantics: a re-issued question
  is a new request with a new `request_id`, and replaying a partial answer would be worse
  than asking again.

## 3. Event order — normative

FR-035 requires a **fixed documented order**. The eight events below are emitted in exactly
this sequence, always, whether the outcome is an answer or a refusal:

| # | Event | Meaning |
|---|---|---|
| 1 | `query_received` | The question was accepted and a request id assigned |
| 2 | `query_analyzed` | Product, category, intent, entities, and rewritten queries determined |
| 3 | `retrieval_started` | Candidate retrieval beginning; announces the pool size |
| 4 | `retrieval_completed` | Candidates returned, with the filters actually applied |
| 5 | `reranking_completed` | Reranking finished, with the reranked count |
| 6 | `generation_started` | Answer generation beginning, with the evidence count |
| 7 | `citation_validation` | Citations checked against the retrieved set |
| 8 | `answer_completed` | The final answer or refusal, with validated citations |

A ninth event, `error`, is terminal and replaces event 8 when the pipeline cannot complete.
No event is ever skipped within 1–7. If a stage produces nothing — zero candidates, a
suppressed filter — its event is still emitted with a zero count. Emitting
`retrieval_completed` with `candidates_retrieved: 0` is how a client distinguishes
"nothing indexed yet" from "nothing relevant found", which the spec requires as distinct
states.

**No token deltas.** There is no `delta` event, and there will not be one. The provider does
not support streaming together with structured output (R-007), so the generated answer is
buffered and delivered whole in `answer_completed` with its citations intact. Progress is
delivered at every stage instead, which is where the wait actually happens: retrieval and
reranking are the slow parts, and both report completion.

## 4. Event payloads

Shapes mirror the `openapi.yaml` component schemas. `null` means *not determined*; `0` means
*determined to be zero*. The distinction matters for classification (FR-009).

### `query_received`

```json
{ "request_id": "0f3c1d2a-6b4e-4c1f-9a77-2b8e5d0c4a11", "question": "How do I rotate an API key?", "inspect": false }
```

### `query_analyzed`

```json
{
  "request_id": "0f3c...",
  "detected_product": "jira",
  "detected_category": "api-tokens",
  "intent": "how_to",
  "entities": ["API key", "rotation"],
  "classification_confidence": { "product": 0.94, "category": 0.88, "intent": 0.91 },
  "rewrite_queries": ["How do I rotate an API key?"],
  "applied_filters": { "product": { "$in": ["jira"] } },
  "filters_suppressed": false,
  "ambiguity_note": null
}
```

- `detected_product`, `detected_category`, `intent` are **`null` when confidence is
  insufficient** — never a guess to fill the field (FR-009).
- When confidence is below the filter threshold, `applied_filters` is `{}` and
  `filters_suppressed` is `true`. A broader search is the correct response to low
  confidence; a narrower one is not (FR-010).
- `rewrite_queries` has more than one entry for multi-part and cross-product questions
  (FR-013). Each entry is retrieved independently.
- `ambiguity_note` is non-`null` when the question spans products. The ambiguity is stated
  rather than silently resolved.

### `retrieval_started`

```json
{ "request_id": "0f3c...", "candidate_pool": 12, "query_count": 1 }
```

`candidate_pool` is the configured pool size (FR-020); `query_count` is how many rewritten
queries are being run against it.

### `retrieval_completed`

```json
{
  "request_id": "0f3c...",
  "candidates_retrieved": 12,
  "queries_executed": [
    { "query": "How do I rotate an API key?", "returned": 12, "top_score": 0.83 }
  ]
}
```

Per-query results are reported separately, so a multi-query expansion that only one half of
which succeeded is visible rather than averaged into a healthy-looking total.

### `reranking_completed`

```json
{ "request_id": "0f3c...", "candidates_reranked": 5, "rerank_model": "bge-reranker-v2-m3" }
```

`candidates_reranked` is the count surviving reranking into the evidence pool, not the count
submitted to it. Both numbers matter: 12 submitted and 5 kept is healthy; 12 submitted and
12 kept means reranking did not discriminate and the configuration is suspect.

### `generation_started`

```json
{ "request_id": "0f3c...", "evidence_count": 5, "attempt": 1 }
```

`attempt` is 1 or 2. Regeneration is capped at two attempts (FR-011), and a second
generation event is the only signal that a retry occurred.

### `citation_validation`

```json
{
  "request_id": "0f3c...",
  "citations_valid": 4,
  "citations_stripped": 1,
  "citations_repaired": 0,
  "rejected_identifiers": ["C7"]
}
```

`rejected_identifiers` lists citation ids the model emitted that were never retrieved. They
were removed before the response was built (FR-003, FR-005, FR-009). Reporting the count
rather than hiding it is deliberate: SC-009 asserts zero *survive*, not that they never
occur, and a reviewer should be able to see that the validator is doing work.

### `answer_completed`

Answered:

```json
{
  "request_id": "0f3c...",
  "outcome": "answered",
  "answer": "Rotate a key by generating a new token and revoking the previous one in the API keys page.",
  "citations": [
    {
      "rank": 1,
      "document_id": "3b1f...",
      "unit_id": "9c4e...",
      "source_url": "https://support.atlassian.com/.../api-tokens/",
      "title": "Manage API tokens",
      "product": "jira",
      "category": "api-tokens",
      "heading_path": ["Manage API tokens", "Rotate a token"],
      "quote": "To rotate a token, create a new one and then revoke the previous token.",
      "retrieval_score": 0.83,
      "rerank_score": 0.91,
      "validation_state": "valid"
    }
  ],
  "total_ms": 3140,
  "confidence": 0.87
}
```

Refused — same event, different outcome. A refusal is a **successful** response, not an error
(FR-008):

```json
{
  "request_id": "0f3c...",
  "outcome": "refused",
  "answer": null,
  "refusal_reason": "INSUFFICIENT_EVIDENCE",
  "searched": {
    "queries": ["How do I rotate an API key?"],
    "products": ["jira"],
    "applied_filters": { "product": { "$in": ["jira"] } },
    "candidates_retrieved": 2,
    "candidates_reranked": 2,
    "evidence_selected": 0
  },
  "leads": [
    {
      "source_url": "https://support.atlassian.com/.../api-tokens/",
      "title": "Manage API tokens",
      "heading_path": ["Manage API tokens"],
      "relevance": 0.31,
      "insufficient": true
    }
  ],
  "total_ms": 2180
}
```

`searched` is mandatory on a refusal — it is what makes the refusal actionable rather than a
dead end (FR-008). `leads` are weakly related sources, each carrying `insufficient: true` as
a literal constant. A lead is never evidence and is never rendered as support for a claim.

### `error` — terminal

```json
{
  "request_id": "0f3c...",
  "code": "VECTOR_SERVICE_UNAVAILABLE",
  "message": "The search service is currently unavailable. No answer was produced. Quote this request id if you report a problem: 0f3c1d2a-6b4e-4c1f-9a77-2b8e5d0c4a11."
}
```

Emitted instead of `answer_completed` when the pipeline cannot finish. `code` is drawn from
the error-code table in [README.md](./README.md).

**An error carries no partial answer.** No partial evidence list, no truncated answer, no
cached prior answer, no keyword-matched substitute. When the embedding or vector service is
unreachable, the response is this error and nothing else (FR-061, FR-062). A degraded answer
that looks complete is the specific failure mode these requirements exist to prevent.

## 5. Termination

```text
data: [DONE]

```

Sent after `answer_completed` or `error`, followed by stream close. A client MUST treat
`[DONE]` as end-of-stream and MUST NOT wait for the connection to close. A stream that ends
without `[DONE]` and without `answer_completed` is a client-visible failure and should be
reported as one, not rendered as a completed answer.

## 6. Client requirements

These are contract obligations on the consumer, not suggestions. Each maps to a specific
silent-failure mode.

| Requirement | Why |
|---|---|
| Read the body with a `ReadableStream` reader | `EventSource` cannot POST |
| `decoder.decode(value, { stream: true })` | Without `stream: true`, multi-byte characters split across chunk boundaries are corrupted |
| `cache: 'no-store'` on the request | Next's fetch cache and the browser cache both break streaming |
| Buffer until a blank line before parsing a frame | A single network read can contain a partial frame |
| Join multiple `data:` lines with `\n` | Required by the SSE grammar even though this contract emits single-line payloads |
| Handle a non-JSON `data` payload without throwing | A proxy error page can arrive mid-stream |
| Treat `answer_completed` and `error` as terminal | Processing past them double-renders |
| Wire an `AbortController` to component teardown | Otherwise unmounting leaves the upstream generation running |

The server MUST set `X-Accel-Buffering: no` and `Cache-Control: no-cache, no-transform`
(R-011). Without them an intermediate proxy buffers the stream and events arrive in a clump
at the end, which looks exactly like a broken backend.

## 7. Prohibited content

**No event in this contract has a reasoning, thought, chain-of-thought, or deliberation
field, and none may be added without a contract change.** FR-034 is absolute.

This is not hypothetical. The default generation models are reasoning models whose API
exposes `reasoning_format` (`hidden` / `raw` / `parsed`) and an `include_reasoning` toggle
(R-014). The defaults are the hazard, not an explicit opt-in. Three controls are required:

1. **Provider call** — `include_reasoning` disabled and `reasoning_format: hidden` set
   explicitly on every call. Never relied upon as a default.
2. **Response parsing** — the model output is unmarshalled into a declared model and
   unrecognised fields are discarded. A raw provider dict is never forwarded to an event, a
   response, or a log line.
3. **Contract test** — a test asserts the event payload schemas contain no reasoning-like
   field and that the generator's request payload cannot request one.

`reasoning_effort` additionally defaults to `medium` on the default models, spending tokens
and wall-clock time on deliberation that is then discarded. The classification path sets it
low, which supports SC-017.

## 8. Accessibility — which events are announced

FR-060 requires streamed progress to reach assistive technology through a live region, or a
screen-reader user sits in silence while the system works.

| Event | Announced | Live-region politeness |
|---|---|---|
| `query_received` | no | — |
| `query_analyzed` | no | — |
| `retrieval_started` | yes — "Searching the indexed documentation" | `polite` |
| `retrieval_completed` | yes — "Found 12 candidate passages" | `polite` |
| `reranking_completed` | yes — "Ranked 5 passages" | `polite` |
| `generation_started` | yes — "Writing the answer" | `polite` |
| `citation_validation` | no | — |
| `answer_completed` | yes — the answer itself | `assertive` |
| `error` | yes | `assertive` |

Classification details, scores, filters, and trace internals are **not** announced. They are
inspection-mode detail for a sighted reviewer (FR-033); announcing raw scores into a live
region would be noise. All events remain available in the DOM and in the inspection panel.

The region uses `aria-live="polite"` with `aria-busy` toggled during generation, so
successive stages are announced without interrupting. `answer_completed` is `assertive`
because the answer is the awaited result.

## 9. Frontend proxy

The Next.js route handler at `frontend/app/api/chat/stream/route.ts` is a **transparent
proxy** to `POST /api/v1/chat/stream`. It forwards the request body, streams the response
through unchanged, and adds nothing.

It is not a second contract and must not transform payloads. Its only responsibilities are
forwarding the backend's `Cache-Control` and `X-Accel-Buffering` headers, and propagating
client disconnect upstream via the stream's `cancel` so an abandoned question stops
generating. Compressors in front of the proxy must be disabled or the stream will buffer
(R-011).
