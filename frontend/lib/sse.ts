/**
 * The SSE reader the UI uses (T129 / R-011, contracts/events.md §6).
 *
 * `EventSource` cannot issue a POST, and a question cannot be a GET, so the
 * reader is `fetch` plus a `ReadableStream` — and the client's obligations in
 * events.md §6 are obligations of *this* file, which is why they are written
 * out rather than assumed of whichever component next needs them.
 *
 * **Buffer until a blank line, then split.** One network read can deliver half a
 * frame, and a frame is only a frame when the blank line after it has arrived.
 * Parsing on every chunk would let a client half-render an event — and the only
 * thing separating a chunk from a frame is a newline the decoder doesn't promise.
 *
 * **`decoder.decode(value, { stream: true })`.** A multi-byte character split
 * across two reads must survive the split, so the decoder carries state.
 *
 * **Multiple `data:` lines join with `\n`.** This contract's frames are single-
 * line by construction (the backend serialises compact JSON), and the reader
 * supports the multi-line form anyway, because a client that only supports the
 * compressed form will silently corrupt a payload the first time an upstream
 * librarypretty-prints one.
 *
 * **A non-JSON payload does not crash the reader.** A proxy error page can
 * arrive mid-stream, and throwing would leave a promise to catch at every call
 * site. The frame is surfaced as raw text, and the caller decides what raw text
 * means.
 *
 * **Exactly one terminal event, then `[DONE]`.** The reader reports the
 * terminal frame once and treats processing past it as a client bug (§6's "never
 * both and never neither" is the reason `error` and `answer_completed` resolve
 * the same promise).
 */

export interface SseFrame {
  /** The event name, or an empty string for the terminator frame. */
  event: string;
  /** Raw `data` text, with multiple `data:` lines joined by `\n`. */
  data: string;
}

export interface SseTerminalFrame extends SseFrame {
  event: "answer_completed" | "error";
}

/** The one frame shape every caller decodes into. */
export type DecodedFrame =
  | { kind: "event"; name: string; data: unknown }
  | { kind: "raw"; name: string; data: string }
  | { kind: "terminal"; data: string };

/**
 * Read a `fetch` SSE response to its terminal frame.
 *
 * Yields each decoded frame. Resolves after the terminal one — never past it.
 * Rejects only for network errors and a non-200 status; contract-level problems
 * (a non-JSON payload, a terminator that never arrives) surface as frames, so
 * the caller can decide what they mean rather than having an exception stand in.
 */
export async function* readSse(
  response: Response,
  signal?: AbortSignal,
): AsyncGenerator<DecodedFrame, void, undefined> {
  if (!response.ok) {
    throw new Error(`the stream endpoint refused with status ${response.status}`);
  }
  const body = response.body;
  if (body === null) {
    throw new Error("the stream endpoint returned no body to read");
  }

  const reader = body.getReader();
  const decoder = new TextDecoder("utf-8", { fatal: false });
  let buffer = "";

  const takeFrame = (block: string): DecodedFrame | null => {
    if (block.trim() === "") {
      return null;
    }
    let eventName = "";
    const dataLines: string[] = [];
    for (const line of block.split("\n")) {
      if (line.startsWith("event: ")) {
        eventName = line.slice("event: ".length).trim();
      } else if (line.startsWith("data: ")) {
        dataLines.push(line.slice("data: ".length));
      } else if (line === "data:") {
        dataLines.push("");
      }
    }
    const raw = dataLines.join("\n");
    if (eventName === "" && raw === "[DONE]") {
      return { kind: "terminal", data: raw };
    }
    try {
      return { kind: "event", name: eventName, data: JSON.parse(raw) };
    } catch {
      // A non-JSON payload (a proxy error page, a truncated frame) must not
      // crash the reader (events.md §6).
      return { kind: "raw", name: eventName, data: raw };
    }
  };

  try {
    for (;;) {
      if (signal?.aborted) {
        return;
      }
      const { done, value } = await reader.read();
      if (done) {
        break;
      }
      buffer += decoder.decode(value, { stream: true });
      let boundary = buffer.indexOf("\n\n");
      while (boundary !== -1) {
        const block = buffer.slice(0, boundary);
        buffer = buffer.slice(boundary + 2);
        const decoded = takeFrame(block);
        if (decoded !== null) {
          yield decoded;
          if (decoded.kind === "terminal" || (decoded.kind === "event" && (decoded.name === "answer_completed" || decoded.name === "error"))) {
            return;
          }
        }
        boundary = buffer.indexOf("\n\n");
      }
    }
  } finally {
    reader.releaseLock();
  }
}
