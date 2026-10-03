/**
 * The one route every browser request goes through: `/api/v1/*` → the backend.
 *
 * **Why a proxy at all.** `BACKEND_BASE_URL` is supplied to this container at
 * *runtime*, and a Next production bundle inlines `NEXT_PUBLIC_*` values at
 * *build* time. There is therefore no way for a browser to learn the backend's
 * URL without a rebuild — so the browser calls same-origin paths and this
 * handler resolves them server-side. That also keeps the API's origin out of the
 * client bundle entirely, which is one less thing to leak and one less CORS
 * configuration to get wrong.
 *
 * **What this is not.** It is not a general proxy and not a security boundary
 * pretending to be one. The destination host is a constant read from the
 * environment; nothing in the request can influence it, so there is no open-
 * proxy or SSRF surface here. What *is* enforced is the path prefix: only
 * `/api/v1/*` is forwarded, so this handler cannot be used to reach a path the
 * application did not intend to expose. Cookies and `Authorization` are never
 * forwarded — this API has no session and no identity, and copying a cookie
 * onto a request would create one by accident.
 *
 * The status, the JSON body, and `X-Response-Request-Id` are returned unchanged,
 * because the client is written against the contract and a proxy that reshaped
 * errors would make every client-side message a guess about the proxy's
 * behaviour rather than the API's.
 */
import { NextResponse, type NextRequest } from "next/server";

/** The contract's prefix. A path outside it is not this application's API. */
const FORWARDED_PREFIX = "/api/v1/";

/** Methods the backend accepts. Anything else is refused rather than forwarded. */
const FORWARDED_METHODS = new Set(["GET", "POST", "PATCH", "DELETE", "OPTIONS"]);

/** Request headers worth passing on: content negotiation and nothing else. */
const FORWARDED_REQUEST_HEADERS = ["content-type", "accept"] as const;

/** Response headers worth passing back: the request id is how an error is traced. */
const FORWARDED_RESPONSE_HEADERS = ["content-type", "x-response-request-id", "retry-after"] as const;

function backendBaseUrl(): string {
  const configured = process.env.BACKEND_BASE_URL;
  if (configured === undefined || configured.trim() === "") {
    throw new Error(
      "BACKEND_BASE_URL is not set. The frontend cannot reach the API without it, and " +
        "there is no default: guessing one is how a misconfigured deployment starts " +
        "talking to whatever happens to be listening.",
    );
  }
  return configured.replace(/\/+$/, "");
}

/**
 * The upstream path for a request, or `null` when it must not be forwarded.
 *
 * Rejection rather than normalisation: `..` in a path is either a mistake or an
 * attempt, and rewriting it into something safe teaches the caller that the
 * rewrite worked. Both the prefix check and the segment check run before any
 * fetch, so a refused request never becomes an outbound one.
 */
function upstreamPath(segments: readonly string[]): string | null {
  const joined = segments.join("/");
  if (joined === "" || joined.includes("..")) {
    return null;
  }
  // The catch-all route mounting already stripped `/api`, so the segments here
  // begin with `v1`. Re-joining with the contract's own `/api` prefix —
  // rather than prepending `/api/v1/` — is what keeps this from doubling the
  // prefix to `/api/v1/v1/...`, a bug a code review could not see but every
  // live request exposed. The prefix check below is then a real check rather
  // than one that a duplicated segment is guaranteed to pass.
  const path = `/api/${joined}`;
  if (path !== "/api/v1" && !path.startsWith(FORWARDED_PREFIX)) {
    return null;
  }
  return path;
}

async function forward(
  request: NextRequest,
  context: { params: Promise<{ path: string[] }> },
): Promise<Response> {
  if (!FORWARDED_METHODS.has(request.method)) {
    return NextResponse.json(
      { error: { code: "VALIDATION_ERROR", message: `Method ${request.method} is not supported.` } },
      { status: 405 },
    );
  }

  const { path } = await context.params;
  const upstream = upstreamPath(path);
  if (upstream === null) {
    return NextResponse.json(
      { error: { code: "NOT_FOUND", message: "No such API path." } },
      { status: 404 },
    );
  }

  const headers = new Headers();
  for (const name of FORWARDED_REQUEST_HEADERS) {
    const value = request.headers.get(name);
    if (value !== null) {
      headers.set(name, value);
    }
  }

  const target = `${backendBaseUrl()}${upstream}${request.nextUrl.search}`;
  const body =
    request.method === "GET" || request.method === "OPTIONS" ? undefined : await request.text();

  let response: Response;
  try {
    response = await fetch(target, {
      method: request.method,
      headers,
      body,
      cache: "no-store",
      // No `credentials`: this API is unauthenticated by design, and forwarding
      // the browser's cookies would attach an identity the backend does not
      // model. `redirect: "manual"` because this API never redirects, and
      // following one would let an upstream response choose the host this
      // handler connects to next.
      redirect: "manual",
    });
  } catch {
    // The backend being unreachable is reported as the API's own vocabulary
    // rather than as an HTML error page from this app, so a client never has to
    // parse two different error formats.
    return NextResponse.json(
      {
        error: {
          code: "INTERNAL_ERROR",
          message: `The backend at ${backendBaseUrl()} could not be reached.`,
        },
      },
      { status: 502 },
    );
  }

  const outbound = new Headers();
  for (const name of FORWARDED_RESPONSE_HEADERS) {
    const value = response.headers.get(name);
    if (value !== null) {
      outbound.set(name, value);
    }
  }
  return new NextResponse(response.body, {
    status: response.status,
    headers: outbound,
  });
}

export const GET = forward;
export const POST = forward;
export const PATCH = forward;
export const DELETE = forward;
export const OPTIONS = forward;

/** A stream is relayed, never buffered (R-011). Node runtime for that reason. */
export const runtime = "nodejs";
export const dynamic = "force-dynamic";