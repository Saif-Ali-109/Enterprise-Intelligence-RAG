"use client";

/**
 * The chat interface (T091): a question, an answer or a refusal, and the sources.
 *
 * **There is no evidence-free rendering path.** The answer text and the citation
 * list are rendered by the same branch, from the same response object: an
 * `outcome: "answered"` response with zero citations renders the citations'
 * empty-message sentence rather than an answer with no visible support. The
 * alternative — a component that can render `answer` on its own — is one edit
 * away from an uncited paragraph, and this is the component where that edit would
 * be made.
 *
 * **A refusal is a first-class state, not an error.** `POST /chat` answers a
 * refusal with HTTP 200, so this panel renders it from the same success path and
 * hands it to `refusal-panel`, which has its own region and no error wording. Only
 * a genuine fault (5xx, unreachable service, a cancelled request) reaches
 * `unavailable-panel`, which names the request id so the failure can be found in a
 * log and in its `query_logs` row (Principle II).
 *
 * **Progress streams.** A question takes seconds because retrieval and reranking
 * are the slow parts, and the panel says which stage it is in from the stream
 * itself — a device that had to guess would tell an inattentive reader the
 * wrong one. The answer completes through the identical `run_chat` pipeline the
 * JSON endpoint serves, and only the transport is the stream.
 *
 * **History is bounded and sent, not faked.** The contract allows ten prior
 * turns; the panel keeps them in memory for the session and sends them with the
 * next question. Nothing is persisted in the browser, because a stored answer
 * would look like one the system can reproduce — and `GET /chat/{request_id}`
 * answers `NOT_FOUND`, so it cannot.
 */
import { useState } from "react";
import type { AskResponse } from "@/lib/api-client";
import { QUESTION_MAX_LENGTH } from "@/hooks/useChat";
import { useAskStream } from "@/hooks/useAskStream";
import { withCitationLinks } from "@/components/chat/citation-links";
import { CrossProductSummary } from "@/components/chat/cross-product-summary";
import { describeSources, summariseSources } from "@/components/chat/source-summary";
import { RefusalPanel } from "@/components/chat/refusal-panel";
import { UnavailablePanel } from "@/components/chat/unavailable-panel";
import { ProgressIndicator } from "@/components/chat/progress-indicator";
import { LiveRegion } from "@/lib/live-region";
import { InspectionTrace } from "@/components/inspection/inspection-trace";
import { Textarea } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";

export function ChatPanel() {
  const [question, setQuestion] = useState("");
  const [history, setHistory] = useState<{ role: "user" | "assistant"; content: string }[]>([]);
  const [inspect, setInspect] = useState(false);
  const stream = useAskStream();

  const trimmed = question.trim();
  const tooLong = question.length > QUESTION_MAX_LENGTH;
  const canAsk = trimmed !== "" && !tooLong && !stream.state.isStreaming;

  function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!canAsk) {
      return;
    }
    const asked = trimmed;
    stream.ask(asked, { inspect, history: history.slice(-10) });
    setHistory((previous) => {
      const next = [...previous, { role: "user" as const, content: asked }];
      return next;
    });
  }

  return (
    <div className="grid gap-6">
      <Card labelledBy="ask-heading">
        <CardHeader>
          <CardTitle id="ask-heading">Ask the indexed documentation</CardTitle>
          <p className="text-sm text-muted-foreground">
            Answers come from the pages registered below and nowhere else. Every substantive
            claim carries a citation, and a question the corpus cannot answer is refused
            rather than guessed at.
          </p>
        </CardHeader>
        <CardContent>
          <form onSubmit={submit} className="grid gap-3">
            <label htmlFor="question" className="text-sm font-medium">
              Your question
            </label>
            <Textarea
              id="question"
              name="question"
              rows={3}
              value={question}
              maxLength={QUESTION_MAX_LENGTH}
              onChange={(event) => setQuestion(event.target.value)}
              placeholder="How do I rotate an API token?"
              aria-describedby="question-help"
            />
            <p id="question-help" className="text-xs text-muted-foreground">
              {tooLong
                ? `${question.length} characters; the limit is ${QUESTION_MAX_LENGTH}.`
                : `${question.length} of ${QUESTION_MAX_LENGTH} characters.`}
            </p>
            <label className="flex items-center gap-2 text-sm text-muted-foreground">
              <input
                type="checkbox"
                checked={inspect}
                onChange={(event) => setInspect(event.target.checked)}
                className="h-4 w-4"
              />
              Show the pipeline trace
            </label>
            <div>
              <Button type="submit" disabled={!canAsk}>
                {stream.state.isStreaming ? "Working…" : "Ask"}
              </Button>
            </div>
          </form>
        </CardContent>
      </Card>

      <LiveRegion
        politeAnnouncement={stream.state.politeAnnouncement}
        assertiveAnnouncement={stream.state.assertiveAnnouncement}
        busy={stream.state.isStreaming}
      />

      <ProgressIndicator stage={stream.state.stage} />

      {stream.state.fault !== null ? <UnavailablePanel error={new Error(stream.state.fault)} /> : null}

      {stream.state.result !== null ? (
        <div className="grid gap-4">
          <AnswerRegion response={stream.state.result} />
          {inspect ? <InspectionTrace frames={stream.state.frames} /> : null}
        </div>
      ) : null}
    </div>
  );
}

/**
 * One response, rendered.
 *
 * Split from `ChatPanel` so the two outcomes are separate code with no shared
 * branch that could render an answer without its citations: the refusal goes
 * straight to `RefusalPanel`, and only the answered branch reaches the answer
 * text *and* the citation list below it.
 */
function AnswerRegion({ response }: { response: AskResponse }) {
  if (response.outcome === "refused") {
    return <RefusalPanel response={response} />;
  }

  return (
    <Card labelledBy="answer-heading">
      <CardHeader>
        <CardTitle id="answer-heading">Answer</CardTitle>
        <p className="text-xs text-muted-foreground">
          {`Asked: ${response.question}`}
          <span aria-hidden="true"> · </span>
          {`${response.timing.total_ms} ms · request ${response.request_id}`}
        </p>
      </CardHeader>
      <CardContent>
        {/*
          `answer` is non-null exactly when `outcome` is `answered`; the fallback
          sentence exists so a malformed response cannot render an empty box that
          reads as "nothing to say". The `[n]` markers inside it are the citation
          ranks the service resolved from the model's inline markers, and they are
          rendered as links to the citation they name — so a claim and its evidence
          are one click apart, which is the point of FR-002.
        */}
        {/*
          How many sources and domains contributed, counted from the validated
          citations rather than reported by the service (T117): the contract has no
          source-count field, and a count derived from the citations cannot
          disagree with them.
        */}
        <p className="text-xs text-muted-foreground">{describeSources(summariseSources(response.citations))}</p>
        <p className="whitespace-pre-wrap text-sm leading-relaxed">
          {withCitationLinks(
            response.answer ?? "The service returned an answer with no text.",
            response.citations.length,
          )}
        </p>
        <div className="mt-4 grid gap-3">
          <CrossProductSummary response={response} />
        </div>
      </CardContent>
    </Card>
  );
}
