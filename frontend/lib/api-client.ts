/**
 * The API client: every shape the backend returns, and one way to ask for it.
 *
 * **Types are transcribed from `specs/001-enterprise-knowledge-rag/contracts/openapi.yaml`,
 * not inferred from a response.** The contract closes every object with
 * `additionalProperties: false`, so a field that appears here and not there is
 * a field a client cannot rely on — and the transcription is checked against the
 * running application by `tests/contract/test_openapi_conformance.py`, which
 * reads the same contract the backend serves. There is no generated client here
 * because there is no generator: hand-written types for a fourteen-endpoint API
 * are shorter than the build step that would produce them, and they can carry a
 * comment saying *why* a field is nullable.
 *
 * **Paths are same-origin and already carry the contract's `/api/v1` prefix.**
 * They are forwarded to the backend by `app/api/[...path]/route.ts`, which holds
 * `BACKEND_BASE_URL` server-side. Nothing in this file knows the backend's
 * address, and no secret is present in it or in anything it imports (FR-042).
 *
 * **Every refusal becomes an `ApiError`.** The backend answers a refusal with a
 * closed `{error: {code, message, request_id, details}}` body, and that shape is
 * the only one this module treats as an error — a component can therefore show
 * the code and the request id, and the request id is what makes the failure
 * traceable to a log line.
 */

/** The eleven error codes in `components.schemas.Error`. */
export type ApiErrorCode =
  | "VALIDATION_ERROR"
  | "SSRF_BLOCKED"
  | "INVALID_URL"
  | "ROBOTS_DISALLOWED"
  | "UNAUTHORIZED"
  | "NOT_FOUND"
  | "CONFLICT"
  | "CRAWL_ALREADY_RUNNING"
  | "DOCUMENT_DELETED"
  | "RATE_LIMITED"
  | "PROVIDER_ERROR"
  | "PROVIDER_RATE_LIMITED"
  | "VECTOR_SERVICE_UNAVAILABLE"
  | "INTERNAL_ERROR";

export interface ApiErrorBody {
  error: {
    code: ApiErrorCode | string;
    message: string;
    request_id?: string;
    details?: Record<string, unknown>;
  };
}

/**
 * A refusal from the API, carrying enough to be reported rather than rephrased.
 *
 * `code` is the stable thing to branch on; `message` is written for a person and
 * must not be parsed. `requestId` is present whenever the backend supplied one,
 * which is every refusal that reached it — a refusal invented here (a network
 * failure) has none, and saying so is better than inventing an id that matches
 * no log line.
 */
export class ApiError extends Error {
  readonly code: ApiErrorCode | string;
  readonly status: number;
  readonly requestId: string | null;
  readonly details: Record<string, unknown>;

  constructor(init: {
    code: ApiErrorCode | string;
    message: string;
    status: number;
    requestId?: string | null;
    details?: Record<string, unknown>;
  }) {
    super(init.message);
    this.name = "ApiError";
    this.code = init.code;
    this.status = init.status;
    this.requestId = init.requestId ?? null;
    this.details = init.details ?? {};
  }

  /** True when the refusal is about this operator's input rather than a fault. */
  get isUserError(): boolean {
    return this.status >= 400 && this.status < 500;
  }
}

/** Offset pagination, exactly as `components.schemas.Page`. */
export interface Page<T> {
  items: T[];
  total: number;
  limit: number;
  offset: number;
}

// ============================================================================
// Chat
// ============================================================================

/**
 * A prior turn. Bounded by the contract at ten messages, and `maxItems` there is
 * a server rule the client does not enforce — the backend refuses with
 * `VALIDATION_ERROR` and the operator can see why.
 */
export interface ChatHistoryMessage {
  role: "user" | "assistant";
  content: string;
}

export interface AskRequest {
  question: string;
  /** Requests the deterministic pipeline trace. Never reasoning (FR-033, FR-034). */
  inspect?: boolean;
  history?: ChatHistoryMessage[];
}

export type RefusalReason =
  | "INSUFFICIENT_EVIDENCE"
  | "UNSUPPORTED_INTENT"
  | "TOO_AMBIGUOUS"
  | "GENERATION_FAILED";

/**
 * One validated citation.
 *
 * `validation_state` is not decoration: `valid` and `repaired` both mean the link
 * came from the registry, and only `repaired` means the model's own link
 * disagreed and was replaced. The two score fields are the two independent
 * signals the contract keeps separate (FR-021) — never averaged, never shown as
 * one number.
 */
export interface Citation {
  rank: number;
  document_id: string;
  unit_id: string;
  source_url: string;
  title: string;
  product: string | null;
  category: string | null;
  heading_path: string[];
  quote: string;
  retrieval_score: number | null;
  rerank_score: number | null;
  validation_state: "valid" | "stripped" | "repaired";
  validation_note: string | null;
}

/**
 * A weakly related source offered on a refusal.
 *
 * `insufficient` is a literal `true` constant: a lead is never evidence and is
 * never rendered as support for a claim. The field exists so that a future
 * renderer cannot quietly promote one.
 */
export interface Lead {
  source_url: string;
  title: string;
  heading_path: string[];
  relevance: number;
  insufficient: true;
}

/** What a refusal searched, which is what makes it actionable (FR-008). */
export interface SearchedScope {
  queries: string[];
  products: (string | null)[];
  applied_filters: Record<string, unknown>;
  candidates_retrieved: number;
  candidates_reranked: number;
  evidence_selected: number;
}

export interface QueryAnalysis {
  original: string;
  normalized?: string;
  detected_product: string | null;
  detected_category: string | null;
  intent: string | null;
  /** Per-field confidence; a low value suppressed filtering rather than narrowing it. */
  confidence: Record<string, number>;
  entities: string[];
  rewrite_queries: string[];
  ambiguity_note: string | null;
}

export interface AskTiming {
  total_ms: number;
  first_event_ms: number | null;
  stages_ms: Record<string, number>;
}

/**
 * The pipeline trace, present only when `inspect` was requested.
 *
 * Free-form because each stage extends it, and deliberately free of any
 * reasoning field: the contract has none and neither has this type (FR-034).
 */
export interface PipelineTrace {
  request_id?: string | null;
  retrieval?: Record<string, unknown> | null;
  reranking?: Record<string, unknown> | null;
  generation?: Record<string, unknown> | null;
  citations?: Record<string, unknown> | null;
}

/**
 * One response: an answer or a refusal, both of them successes.
 *
 * The two outcomes share a shape on purpose (Principle II), so a client has one
 * render path and cannot present a refusal as an error. `outcome` is what to
 * branch on; `answer` is `null` exactly when `outcome` is `refused`.
 */
export interface AskResponse {
  request_id: string;
  question: string;
  outcome: "answered" | "refused";
  answer: string | null;
  refusal_reason: RefusalReason | null;
  searched?: SearchedScope | null;
  leads: Lead[];
  citations: Citation[];
  query_analysis?: QueryAnalysis | null;
  timing: AskTiming;
  trace?: PipelineTrace | null;
}

// ============================================================================
// Sources
// ============================================================================

export type SourceStatus = "active" | "disabled" | "error";

export interface Source {
  id: string;
  name: string;
  start_url: string;
  allowed_domains: string[];
  product_domain: string | null;
  enabled: boolean;
  status: SourceStatus;
  /** Live documents; tombstones excluded. */
  page_count: number;
  unit_count: number;
  last_crawl_at: string | null;
  last_crawl_status: CrawlJobStatus | null;
  created_at: string;
}

export interface SourceCreate {
  name: string;
  start_url: string;
  /** Null for a cross-product source. Never forced by the client (FR-013). */
  product_domain?: string | null;
  max_pages?: number;
  max_depth?: number;
  delay_seconds?: number;
  start_crawl?: boolean;
}

/**
 * `PATCH` takes only what it changes.
 *
 * "Absent" and "explicitly null" are different requests — the backend reads
 * `model_fields_set` for exactly this reason — so `product_domain: null` clears
 * a classification and omitting the key leaves it alone. A client that sent
 * every field would erase a classification each time it renamed a source.
 */
export interface SourceUpdate {
  name?: string;
  enabled?: boolean;
  product_domain?: string | null;
  max_pages?: number;
  max_depth?: number;
  delay_seconds?: number;
}

/** The outcome of registering: `created` distinguishes 201 from 200. */
export interface RegisterSourceResult {
  source: Source;
  crawlJob: CrawlJob | null;
  created: boolean;
}

// ============================================================================
// Documents and units
// ============================================================================

export type DocumentState =
  | "discovered"
  | "crawling"
  | "processed"
  | "indexed"
  | "failed"
  | "deleted";

export interface Document {
  id: string;
  source_id: string;
  url: string;
  canonical_url: string | null;
  title: string | null;
  product: string | null;
  category: string | null;
  page_type: string | null;
  language: string | null;
  state: DocumentState;
  /** The failure code, narrowed to `code` by the serialiser — no free detail. */
  state_detail: string | null;
  /** Whether this document's vectors exist, independent of `state` (FR-054). */
  vectors_live: boolean;
  unit_count: number;
  content_fingerprint: string | null;
  source_modified_at: string | null;
  last_crawled_at: string | null;
  indexed_at: string | null;
  tombstoned_at: string | null;
  created_at: string;
}

export interface DocumentUnit {
  id: string;
  document_id: string;
  ordinal: number;
  /** `{document_id}#{ordinal:04d}` — immutable across re-crawls (R-005). */
  vector_id: string;
  /**
   * The full heading path, outermost first (R-009).
   *
   * Never collapsed to a string by the client: a citation that renders
   * `Boards` where the path was `Boards > Configuration > Fields` points the
   * reader at the section rather than the page, which is the misattribution
   * FR-018 exists to prevent.
   */
  heading_path: string[];
  block_types: string[];
  token_count: number;
  overlap_tokens: number;
  text_fingerprint: string;
}

// ============================================================================
// Crawl jobs
// ============================================================================

export type CrawlJobStatus =
  | "queued"
  | "running"
  | "completed"
  | "completed_with_errors"
  | "failed"
  | "cancelled";

export interface CrawlJobCounters {
  discovered: number;
  processed: number;
  unchanged: number;
  skipped: number;
  failed: number;
}

/**
 * `completed_with_errors` is a state of its own, and it is rendered as one.
 *
 * Collapsing it into `completed` would make a crawl that failed half its pages
 * indistinguishable from a clean one; collapsing it into `failed` would hide the
 * fact that most of the corpus indexed fine (FR-054).
 */
export interface CrawlJob {
  id: string;
  source_id: string;
  target_url: string;
  status: CrawlJobStatus;
  requested_at: string;
  started_at: string | null;
  finished_at: string | null;
  counters: CrawlJobCounters;
  /** `{robots_disallowed: 3, too_small: 1}` — why pages were not processed. */
  skipped_reasons: Record<string, number> | null;
  error_code: string | null;
  error_message: string | null;
}

// ============================================================================
// Configuration and health
// ============================================================================

/** Presence only. There is deliberately no `value` anywhere in this shape. */
export interface SecretPresence {
  configured: boolean;
}

export interface CorpusSummary {
  source_count: number;
  document_count: number;
  unit_count: number;
  allowed_domains: string[];
}

export interface ConfigResponse {
  retrieval: {
    candidate_pool: number;
    rerank_top_n: number;
    evidence_min: number;
    evidence_max: number;
    filters_enabled: boolean;
    min_rerank_score?: number;
    min_evidence_score?: number;
  };
  generation: {
    provider: string;
    model: string;
    classification_model: string;
    max_attempts: number;
    /** `const: false` in the contract — an operator can verify FR-034 live. */
    reasoning_exposed: boolean;
  };
  index: {
    name: string;
    namespace: string;
    embed_model: string;
    dimension_source: string;
    rerank_model: string;
  };
  corpus: CorpusSummary;
  secrets: Record<string, SecretPresence>;
}

export type DependencyState = "ok" | "degraded" | "unavailable";

export interface DependencyReport {
  status: DependencyState;
  detail: string | null;
  latency_ms: number | null;
}

export interface HealthResponse {
  status: DependencyState;
  version: string | null;
  dependencies: Record<string, DependencyReport>;
}

// ============================================================================
// Evaluation
// ============================================================================

export type EvaluationRunStatus = "queued" | "running" | "completed" | "failed";

/**
 * The metric block is null until a run has finished (FR-036).
 *
 * `null` and `0` are different claims: `null` is "this run has no number here",
 * `0` is "the measurement said zero". The dashboard renders them differently, so
 * the client keeps them apart instead of coalescing.
 */
export interface EvaluationMetrics {
  recall_at_5: number | null;
  precision_at_5: number | null;
  mrr: number | null;
  cross_product_domain_coverage: number | null;
  citation_validity: number | null;
  claim_coverage: number | null;
  faithfulness: number | null;
  unsupported_refusal_rate: number | null;
  /** A count, never a ratio (FR-066). */
  fabricated_fact_count: number | null;
  /** A count, never a ratio (FR-066). */
  invalid_citation_count: number | null;
  p50_latency_ms: number | null;
  p95_latency_ms: number | null;
  error_rate: number | null;
}

export interface EvaluationGateFailure {
  gate: string;
  target: number;
  actual: number;
}

export interface EvaluationRun {
  id: string;
  dataset_version: string;
  dataset_fingerprint: string;
  status: EvaluationRunStatus;
  started_at: string | null;
  finished_at: string | null;
  question_count: number;
  metrics: EvaluationMetrics | null;
  thresholds: Record<string, number>;
  gate_outcome: "pass" | "fail";
  gate_failures: EvaluationGateFailure[];
}

export interface EvaluationResult {
  question_id: string;
  question: string | null;
  category: string | null;
  difficulty: string | null;
  recall_at_5: number | null;
  precision_at_5: number | null;
  mrr: number | null;
  citation_validity: number | null;
  claim_coverage: number | null;
  faithfulness: number | null;
  refused: boolean;
  fabricated_fact_count: number;
  invalid_citation_count: number;
  latency_ms: number | null;
  error: string | null;
}

export interface EvaluationQuestion {
  id: string;
  question: string;
  category: string;
  difficulty: string;
  is_unsupported: boolean;
}

// ============================================================================
// The request
// ============================================================================

const API_PREFIX = "/api/v1";

interface RequestOptions {
  method?: "GET" | "POST" | "PATCH" | "DELETE";
  /** Serialised as JSON. Omitted entirely for a request with no body. */
  body?: unknown;
  signal?: AbortSignal;
}

/**
 * One `fetch`, one error contract.
 *
 * A non-2xx response is parsed as the API's error envelope and re-thrown as an
 * `ApiError`; a 2xx response with an unparseable body is an `ApiError` too,
 * because a component handed `undefined` where a source should be will fail
 * somewhere less informative. Status 204 carries no body and is handled by the
 * caller's return type.
 *
 * Named `send`, not `request`, because every method below takes a parameter
 * called `request` — and a module-level `request` would be shadowed by it, so
 * `request<Page<Source>>(...)` inside `sources.list` would have been a call to
 * the parameter. The compiler caught it; a name that can be shadowed by the
 * calling convention is the actual defect.
 */
async function send<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { method = "GET", body, signal } = options;
  const headers: Record<string, string> = { Accept: "application/json" };
  if (body !== undefined) {
    headers["Content-Type"] = "application/json";
  }

  let response: Response;
  try {
    response = await fetch(`${API_PREFIX}${path}`, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      signal,
      // The browser must not serve an API answer from its cache: a source list
      // that looks unchanged after a crawl is worse than one that refetches.
      cache: "no-store",
    });
  } catch (cause) {
    if (cause instanceof DOMException && cause.name === "AbortError") {
      throw cause;
    }
    throw new ApiError({
      code: "INTERNAL_ERROR",
      message: "The service could not be reached. It may be starting, or stopped.",
      status: 0,
    });
  }

  if (response.status === 204) {
    return undefined as T;
  }

  const text = await response.text();
  let parsed: unknown = null;
  if (text !== "") {
    try {
      parsed = JSON.parse(text);
    } catch {
      throw new ApiError({
        code: "INTERNAL_ERROR",
        message: `The service returned a ${response.status} response that is not JSON.`,
        status: response.status,
      });
    }
  }

  if (!response.ok) {
    const envelope = parsed as Partial<ApiErrorBody> | null;
    const error = envelope?.error;
    throw new ApiError({
      code: typeof error?.code === "string" ? error.code : "INTERNAL_ERROR",
      message:
        typeof error?.message === "string" && error.message !== ""
          ? error.message
          : `The service refused the request with status ${response.status}.`,
      status: response.status,
      requestId: typeof error?.request_id === "string" ? error.request_id : null,
      details: error?.details ?? {},
    });
  }

  return parsed as T;
}

/**
 * Query strings with `undefined` dropped rather than serialised as `"undefined"`.
 *
 * The parameter is typed `object`, not a `Record`: the request shapes are
 * interfaces, and an interface without an index signature is not assignable to
 * one — so a `Record` parameter would have rejected every call site and the fix
 * would have been to widen them all. Values are re-narrowed here, and anything
 * that is not a string, number, or boolean is dropped rather than coerced: a
 * nested object serialised into a query string is `?filter=[object Object]`,
 * which the API would refuse with a validation error three layers away from the
 * mistake.
 */
function query(params: object): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params as Record<string, unknown>)) {
    if (typeof value !== "string" && typeof value !== "number" && typeof value !== "boolean") {
      continue;
    }
    if (value === "") {
      continue;
    }
    search.set(key, String(value));
  }
  const rendered = search.toString();
  return rendered === "" ? "" : `?${rendered}`;
}

export interface PageRequest {
  limit?: number;
  offset?: number;
}

export interface SourceListRequest extends PageRequest {
  enabled?: boolean;
}

export interface DocumentListRequest extends PageRequest {
  source_id?: string;
  state?: DocumentState;
  product?: string;
  category?: string;
}

export type UnitListRequest = PageRequest;

export interface CrawlJobListRequest extends PageRequest {
  source_id?: string;
  status?: CrawlJobStatus;
}

// ============================================================================
// The methods
// ============================================================================

/**
 * Sources.
 *
 * `registerSource` reads the status code rather than guessing from the body:
 * the contract answers a *new* source with `{source, crawl_job}` and an
 * *existing* one with a bare `Source`, and the only reliable way to tell them
 * apart is 201 versus 200. Both are normal outcomes — registering a URL that is
 * already registered is idempotent by design, not a failure — so neither throws.
 */
export const sources = {
  list(request: SourceListRequest = {}, signal?: AbortSignal): Promise<Page<Source>> {
    return send<Page<Source>>(`/sources${query(request)}`, { signal });
  },

  async register(
    payload: SourceCreate,
    signal?: AbortSignal,
  ): Promise<RegisterSourceResult> {
    const response = await fetch(`${API_PREFIX}/sources`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify(payload),
      signal,
      cache: "no-store",
    });
    const body = (await readJson(response)) as
      | { source: Source; crawl_job?: CrawlJob | null }
      | Source
      | null;

    if (!response.ok) {
      throw errorFrom(response, body);
    }
    if (response.status === 200) {
      // Already registered: the bare `Source`, and no job was started.
      return { source: body as Source, crawlJob: null, created: false };
    }
    const registered = body as { source: Source; crawl_job?: CrawlJob | null };
    return { source: registered.source, crawlJob: registered.crawl_job ?? null, created: true };
  },

  get(id: string, signal?: AbortSignal): Promise<Source> {
    return send<Source>(`/sources/${encodeURIComponent(id)}`, { signal });
  },

  update(id: string, payload: SourceUpdate, signal?: AbortSignal): Promise<Source> {
    return send<Source>(`/sources/${encodeURIComponent(id)}`, {
      method: "PATCH",
      body: payload,
      signal,
    });
  },

  remove(id: string, signal?: AbortSignal): Promise<void> {
    return send<void>(`/sources/${encodeURIComponent(id)}`, { method: "DELETE", signal });
  },

  /** Request a re-crawl. Refused with `CRAWL_ALREADY_RUNNING` if one is live. */
  crawl(id: string, signal?: AbortSignal): Promise<CrawlJob> {
    return send<CrawlJob>(`/sources/${encodeURIComponent(id)}/crawl`, {
      method: "POST",
      signal,
    });
  },
};

export const documents = {
  list(request: DocumentListRequest = {}, signal?: AbortSignal): Promise<Page<Document>> {
    return send<Page<Document>>(`/documents${query(request)}`, { signal });
  },

  get(id: string, signal?: AbortSignal): Promise<Document> {
    return send<Document>(`/documents/${encodeURIComponent(id)}`, { signal });
  },

  remove(id: string, signal?: AbortSignal): Promise<void> {
    return send<void>(`/documents/${encodeURIComponent(id)}`, { method: "DELETE", signal });
  },

  /** A document's units in ordinal order, without their text. */
  units(id: string, request: UnitListRequest = {}, signal?: AbortSignal): Promise<
    Page<DocumentUnit>
  > {
    return send<Page<DocumentUnit>>(
      `/documents/${encodeURIComponent(id)}/units${query(request)}`,
      { signal },
    );
  },
};

export const crawlJobs = {
  list(request: CrawlJobListRequest = {}, signal?: AbortSignal): Promise<Page<CrawlJob>> {
    return send<Page<CrawlJob>>(`/crawl-jobs${query(request)}`, { signal });
  },

  get(id: string, signal?: AbortSignal): Promise<CrawlJob> {
    return send<CrawlJob>(`/crawl-jobs/${encodeURIComponent(id)}`, { signal });
  },
};

/**
 * Chat.
 *
 * `ask` is the non-streaming path the contract defines for `POST /chat`, and it
 * is the one the UI calls today: the answer arrives whole, with its citations
 * intact, because there are no token deltas to render and a partial citation
 * list is worse than a short wait (contracts/events.md §3).
 *
 * `get` is `GET /chat/{request_id}`. It exists because the contract declares it,
 * and the client exposes it rather than hiding it: a backend that answers only
 * live returns `NOT_FOUND`, and the UI's handling of that is the same handling
 * any other refusal gets.
 */
export const chat = {
  ask(request: AskRequest, signal?: AbortSignal): Promise<AskResponse> {
    return send<AskResponse>("/chat", { method: "POST", body: request, signal });
  },

  get(requestId: string, signal?: AbortSignal): Promise<AskResponse> {
    return send<AskResponse>(`/chat/${encodeURIComponent(requestId)}`, { signal });
  },
};

/**
 * Operational reads.
 *
 * `config` carries secrets as presence booleans only (FR-042) — the client has
 * no field that could hold a value, which is why the schema has none either.
 * `health` reports each dependency independently so a vector outage can be read
 * as a vector outage rather than as a generic failure.
 */
export const system = {
  config(signal?: AbortSignal): Promise<ConfigResponse> {
    return send<ConfigResponse>("/config", { signal });
  },
  health(signal?: AbortSignal): Promise<HealthResponse> {
    return send<HealthResponse>("/health", { signal });
  },
};

/**
 * Evaluation.
 *
 * A run is asynchronous, so `start` returns a row whose `status` is still
 * `running` and whose `metrics` is still null. That is the contract's answer,
 * not a gap in the client: the dashboard polls `get` until `status` leaves the
 * in-flight states, and shows nothing quantitative before then.
 */
export const evaluations = {
  dataset(signal?: AbortSignal): Promise<{ dataset_version: string; questions: EvaluationQuestion[] }> {
    return send<{ dataset_version: string; questions: EvaluationQuestion[] }>("/evaluations/dataset", {
      signal,
    });
  },

  listRuns(request: PageRequest = {}, signal?: AbortSignal): Promise<Page<EvaluationRun>> {
    return send<Page<EvaluationRun>>(`/evaluations/runs${query(request)}`, { signal });
  },

  startRun(signal?: AbortSignal): Promise<EvaluationRun> {
    return send<EvaluationRun>("/evaluations/runs", { method: "POST", signal });
  },

  getRun(runId: string, signal?: AbortSignal): Promise<EvaluationRun> {
    return send<EvaluationRun>(`/evaluations/runs/${encodeURIComponent(runId)}`, { signal });
  },

  listResults(runId: string, request: PageRequest = {}, signal?: AbortSignal): Promise<
    Page<EvaluationResult>
  > {
    return send<Page<EvaluationResult>>(
      `/evaluations/runs/${encodeURIComponent(runId)}/results${query(request)}`,
      { signal },
    );
  },
};

// ============================================================================
// Helpers shared by `registerSource` and the generic path
// ============================================================================

async function readJson(response: Response): Promise<unknown> {
  const text = await response.text();
  if (text === "") {
    return null;
  }
  try {
    return JSON.parse(text);
  } catch {
    return null;
  }
}

/**
 * Turn a non-2xx response into the same `ApiError` the generic path raises.
 *
 * Registration reads the response itself rather than reusing `request`, because
 * it is the only endpoint whose success body differs by status code. Sharing the
 * error construction is what keeps the two paths from disagreeing about what an
 * error looks like.
 */
function errorFrom(response: Response, body: unknown): ApiError {
  const envelope = body as Partial<ApiErrorBody> | null;
  const error = envelope?.error;
  return new ApiError({
    code: typeof error?.code === "string" ? error.code : "INTERNAL_ERROR",
    message:
      typeof error?.message === "string" && error.message !== ""
        ? error.message
        : `The service refused the request with status ${response.status}.`,
    status: response.status,
    requestId: typeof error?.request_id === "string" ? error.request_id : null,
    details: error?.details ?? {},
  });
}

/** Whether an unknown thrown value is an `ApiError`, for `instanceof`-free checks. */
export function isApiError(value: unknown): value is ApiError {
  return value instanceof ApiError;
}

/** The message to show for any thrown value, including a cancellation. */
export function describeError(value: unknown): string {
  if (isApiError(value)) {
    return value.requestId === null
      ? value.message
      : `${value.message} (request ${value.requestId})`;
  }
  if (value instanceof Error) {
    return value.message;
  }
  return "An unexpected error occurred.";
}