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