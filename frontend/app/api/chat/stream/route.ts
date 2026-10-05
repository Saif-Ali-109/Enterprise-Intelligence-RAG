/**
 * Transparent proxy for the SSE stream (T133 / R-011, events.md §1).
 *
 * **Why this path exists alongside `/[...path]`.** The catch-all proxy forwards
 * any `/api/v1/*` path, and it forwards the body stream rather than buffering —
 * but it has no business knowing what a stream *is*. A reader whose contract with
 * the backend is "I read SSE frames" deserves a hop that says so in its path, and
 * this route is that hop: `/api/chat/stream` on the browser's side maps to
 * `/api/v1/chat/stream` upstream, unchanged.
 *
 * **Transparent is the contract.** The upstream body is relayed as a
 * `ReadableStream` rather than read into memory, and the frame boundary — the
 * blank line between events — is preserved exactly. A proxy that parsed the
 * stream and re-emitted it would be a second implementation of the framing,
 * and every buffering decision it made would arrive on the client wearing the
 * backend's contract. This route makes no framing decisions at all.
 *
 * **The error vocabulary is the API's, not the proxy's.** An unreachable backend
 * is the same envelope the JSON proxy produces, because a client parsing one
 * error format for the whole UI does not need to know which route it used.
 */
import { NextResponse, type NextRequest } from "next/server";

const UPSTREAM = "/api/v1/chat/stream";

export async function POST(request: NextRequest): Promise<Response> {
  const base = process.env.BACKEND_BASE_URL;
  if (base === undefined || base.trim() === "") {
    throw new Error(
      "BACKEND_BASE_URL is not set. The frontend cannot reach the API without it, and " +
        "there is no default: guessing one is how a misconfigured deployment starts " +
        "talking to whatever happens to be listening.",
    );
  }

  const contentType = request.headers.get("content-type") ?? "application/json";
  const accept = request.headers.get("accept") ?? "text/event-stream";
  const bodyText = await request.text();

  let upstream: Response;
  try {
    upstream = await fetch(`${base.replace(/\/+$/, "")}${UPSTREAM}`, {
      method: "POST",
      headers: { "content-type": contentType, accept },
      body: bodyText,
      cache: "no-store",
      redirect: "manual",
    });
  } catch {
    return NextResponse.json(
      { error: { code: "INTERNAL_ERROR", message: `The backend at ${base} could not be reached.` } },
      { status: 502 },
    );
  }

  // The body is relayed, never collected: this is the difference between a
  // stream and a slow download (R-011, contracts/events.md §1).
  return new Response(upstream.body, {
    status: upstream.status,
    headers: {
      "content-type": upstream.headers.get("content-type") ?? "text/event-stream; charset=utf-8",
      "cache-control": "no-cache, no-transform",
      "x-accel-buffering": "no",
    },
  });
}

export const runtime = "nodejs";
export const dynamic = "force-dynamic";
