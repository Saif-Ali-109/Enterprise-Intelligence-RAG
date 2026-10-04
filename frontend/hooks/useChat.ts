"use client";

/**
 * React Query hooks for chat.
 *
 * **A question is a mutation, not a query.** A query is keyed by its input and
 * is safe to replay; asking the same question twice is not the same operation,
 * and the answer depends on a corpus that a crawl may have changed since. So
 * `useAsk` is a mutation whose success is cached under the *response's*
 * `request_id` — the one stable identifier a response has — and never under the
 * question text.
 *
 * **The answer is kept whole.** There is no token stream and no partial state to
 * reconcile: the contract has no `delta` event, the provider does not stream with
 * structured output, and a half-rendered citation list is worse than a short
 * wait. `isPending` is the only progress the hook exposes, and the panel pairs it
 * with a sentence that names the stage rather than a spinner with no context.
 *
 * **A refusal is a success.** `POST /chat` answers a refusal with 200 and
 * `outcome: "refused"`, so this hook resolves for refusals and rejects only for
 * genuine faults (a 5xx, an unreachable service, a cancelled request). A UI that
 * branched on `isError` to decide whether the question was answered would show
 * every refusal as an error, which is the failure Principle II exists to prevent.
 */
import {
  useMutation,
  useQuery,
  useQueryClient,
  type UseMutationResult,
  type UseQueryResult,
} from "@tanstack/react-query";
import {
  chat,
  type AskRequest,
  type AskResponse,
  type ChatHistoryMessage,
} from "@/lib/api-client";

/** The contract's bound on a question. Enforced again by the backend. */
export const QUESTION_MAX_LENGTH = 2000;

/** The contract's bound on prior turns. */
export const HISTORY_MAX_MESSAGES = 10;

export interface AskVariables {
  question: string;
  /** Requests the deterministic trace; never reasoning (FR-034). */
  inspect?: boolean;
  history?: ChatHistoryMessage[];
}

export function useAsk(): UseMutationResult<AskResponse, Error, AskVariables> {
  return useMutation({
    mutationFn: ({ question, inspect, history }: AskVariables): Promise<AskResponse> => {
      const request: AskRequest = {
        question,
        ...(inspect === undefined ? {} : { inspect }),
        ...(history === undefined ? {} : { history }),
      };
      return chat.ask(request);
    },
  });
}

/**
 * Re-read a recorded answer by its request id.
 *
 * Disabled unless given an id, because the endpoint is `GET /chat/{request_id}`
 * and there is no such thing as asking for "the last answer". A backend that
 * answers only live returns `NOT_FOUND`, which arrives here as an `ApiError` and
 * is rendered like any other refusal rather than as a failure of the page.
 */
export function useRecordedAnswer(
  requestId: string | null,
): UseQueryResult<AskResponse, Error> {
  const client = useQueryClient();
  return useQuery({
    queryKey: ["chat", requestId],
    queryFn: ({ signal }) => chat.get(requestId ?? "", signal),
    enabled: requestId !== null,
    staleTime: Number.POSITIVE_INFINITY,
    // The same answer cached from a mutation and re-read through this hook must
    // not disagree, so a successful `useAsk` seeds the cache here.
    initialData: () => {
      const cached = client.getQueryData<AskResponse>(["chat", requestId]);
      return cached ?? undefined;
    },
  });
}

/**
 * A refusal, in the words a person can act on.
 *
 * The code is a contract constant and is never parsed into a different refusal;
 * this is the only place the four reasons get prose, so the wording cannot drift
 * between the panel and the citation view.
 */
export function describeRefusal(reason: string | null): string {
  switch (reason) {
    case "INSUFFICIENT_EVIDENCE":
      return "The indexed documentation did not provide enough evidence to answer this.";
    case "UNSUPPORTED_INTENT":
      return "This system answers questions about the indexed documentation, and this one asks for something else.";
    case "TOO_AMBIGUOUS":
      return "The question spans more than one interpretation, and answering one of them would be a guess.";
    case "GENERATION_FAILED":
      return "No grounded answer could be produced. Nothing was guessed in its place.";
    default:
      return "The indexed documentation did not provide an answer.";
  }
}