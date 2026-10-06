"use client";

/**
 * Ask a question over the stream, and keep the UI state an operator reads (T132).
 *
 * **One request, one answer.** A question is *not* replayable, so unlike
 * `useAsk` this is not a query keyed by its input: it is a manual start, an
 * in-flight model of the frames, and one terminal result. The frames arrive in
 * contract order; the terminal one resolves the answer.
 *
 * **The state model has exactly the shape the contract gives.** The model never
 * invents intermediate answers: until `answer_completed` (or `error`) there is
 * no `answer` field at all, which the type reflects — `result` stays `null`, and
 * what exists is the list of stage frames the pipeline has actually emitted. An
 * operator during those seconds sees progress, and what they never see is a
 * partial answer dressed as a complete one (FR-061, FR-062).
 *
 * **Accessibility: exactly the contracts/events.md §8 set is announced.** What
 * is announced, and what politeness, lives in `announcementFor` — one function,
 * one reason for changing, rather than per-component `aria-live` writing.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import type { AskResponse } from "@/lib/api-client";
import { readSse } from "@/lib/sse";

export interface StreamFrameRecord {
  name: string;
  data: Record<string, unknown> | string;
}

export interface AskStreamState {
  /** True from the start call until the terminal frame or a fault. */
  isStreaming: boolean;
  /** Every frame received, in order. The inspection view renders this. */
  frames: StreamFrameRecord[];
  /** The human sentence for the stage in progress, or null once finished. */
  stage: string | null;
  /** Last polite announcement, for the polite region (which persists). */
  politeAnnouncement: string | null;
  /** Last assertive announcement, for the assertive region (outcome or fault). */
  assertiveAnnouncement: string | null;
  /** The answer or refusal, present exactly once, after the terminal frame. */
  result: AskResponse | null;
  /** A transport-level fault (not a refusal): status refused, network died. */
  fault: string | null;
}

/** Exactly the set events.md §8 announces, and the politeness it assigns. */
export function announcementFor(
  name: string,
  data: Record<string, unknown>,
): { text: string; politeness: "polite" | "assertive" } | null {
  switch (name) {
    case "retrieval_started":
      return { text: "Searching the indexed documentation", politeness: "polite" };
    case "retrieval_completed":
      return {
        text: `Found ${String(data["candidates_retrieved"])} candidate passages`,
        politeness: "polite",
      };
    case "reranking_completed":
      return {
        text: `Ranked ${String(data["candidates_reranked"])} passages`,
        politeness: "polite",
      };
    case "generation_started":
      return { text: "Writing the answer", politeness: "polite" };
    case "answer_completed":
      return {
        text: data["outcome"] === "answered" ? "Answer ready" : "No answer could be given",
        politeness: "assertive",
      };
    case "error":
      return { text: "Something failed while answering", politeness: "assertive" };
    default:
      // query_received, query_analyzed, citation_validation: detail, not noise.
      return null;
  }
}

/** The one-line stage text the progress indicator shows during a stage. */
export function stageFor(name: string | null): string | null {
  switch (name) {
    case "retrieval_started":
      return "Searching the indexed documentation…";
    case "retrieval_completed":
      return "Ranking the candidates…";
    case "reranking_completed":
      return "Writing the answer…";
    case "generation_started":
      return "Writing the answer…";
    case "citation_validation":
      return "Checking citations…";
    default:
      return null;
  }
}

export function useAskStream() {
  const [state, setState] = useState<AskStreamState>({
    isStreaming: false,
    frames: [],
    stage: null,
    politeAnnouncement: null,
    assertiveAnnouncement: null,
    result: null,
    fault: null,
  });
  const abortRef = useRef<AbortController | null>(null);

  const stop = useCallback(() => {
    abortRef.current?.abort();
    abortRef.current = null;
    setState((previous) => ({ ...previous, isStreaming: false }));
  }, []);

  const ask = useCallback((question: string, options: { inspect?: boolean; history?: {role: "user" | "assistant"; content: string}[] } = {}) => {
    const controller = new AbortController();
    abortRef.current?.abort();
    abortRef.current = controller;

    setState({
      isStreaming: true,
      frames: [],
      stage: "Searching the indexed documentation…",
      politeAnnouncement: "Question received",
      assertiveAnnouncement: null,
      result: null,
      fault: null,
    });

    (async () => {
      let response: Response;
      try {
        response = await fetch("/api/chat/stream", {
          method: "POST",
          headers: { "content-type": "application/json", accept: "text/event-stream" },
          body: JSON.stringify({
            question,
            ...(options.inspect === undefined ? {} : { inspect: options.inspect }),
            ...(options.history === undefined ? {} : { history: options.history }),
          }),
          signal: controller.signal,
          cache: "no-store",
        });
      } catch (cause) {
        setState((previous) => ({
          ...previous,
          isStreaming: false,
          fault: cause instanceof Error ? cause.message : "The request could not be sent.",
        }));
        return;
      }

      try {
        for await (const frame of readSse(response, controller.signal)) {
          if (frame.kind === "event") {
            const data = frame.data as Record<string, unknown>;
            const announcement = announcementFor(frame.name, data);
            setState((previous) => ({
              ...previous,
              frames: [...previous.frames, { name: frame.name, data }],
              stage: stageFor(frame.name),
              politeAnnouncement:
                announcement?.politeness === "polite"
                  ? announcement.text
                  : previous.politeAnnouncement,
              assertiveAnnouncement:
                announcement?.politeness === "assertive"
                  ? announcement.text
                  : previous.assertiveAnnouncement,
            }));
            if (frame.name === "answer_completed") {
              setState((previous) => ({
                ...previous,
                isStreaming: false,
                stage: null,
                result: toAskResponse(data, question),
              }));
            } else if (frame.name === "error") {
              setState((previous) => ({
                ...previous,
                isStreaming: false,
                stage: null,
                fault:
                  typeof data["message"] === "string"
                    ? (data["message"] as string)
                    : "The pipeline answered with an error.",
              }));
            }
          } else if (frame.kind === "raw") {
            setState((previous) => ({
              ...previous,
              frames: [...previous.frames, { name: frame.name, data: frame.data }],
            }));
          }
        }
      } catch (cause) {
        if (controller.signal.aborted) {
          setState((previous) => ({ ...previous, isStreaming: false, fault: null }));
          return;
        }
        setState((previous) => ({
          ...previous,
          isStreaming: false,
          fault: cause instanceof Error ? cause.message : "The stream was interrupted.",
        }));
      } finally {
        setState((previous) => (previous.isStreaming ? { ...previous, isStreaming: false } : previous));
      }
    })();
  }, []);

  useEffect(() => () => abortRef.current?.abort(), []);

  return { ask, stop, state };
}

function toAskResponse(data: Record<string, unknown>, question: string): AskResponse {
  const outcome = data["outcome"] === "answered" ? "answered" : "refused";
  const total = typeof data["total_ms"] === "number" ? data["total_ms"] : 0;
  return {
    request_id: String(data["request_id"] ?? ""),
    question,
    outcome,
    answer: typeof data["answer"] === "string" ? data["answer"] : null,
    refusal_reason: (data["refusal_reason"] as AskResponse["refusal_reason"]) ?? null,
    searched: (data["searched"] as AskResponse["searched"]) ?? null,
    leads: (data["leads"] as AskResponse["leads"]) ?? [],
    citations: (data["citations"] as AskResponse["citations"]) ?? [],
    query_analysis: null,
    timing: { total_ms: total, first_event_ms: null, stages_ms: {} },
    trace: null,
  };
}
