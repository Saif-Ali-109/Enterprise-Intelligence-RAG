import { describeError, isApiError } from "@/lib/api-client";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";

/**
 * The service being unable to answer, rendered as a fault (T109 / FR-061, FR-062).
 *
 * **It never shows an answer.** The backend returns no answer when the vector
 * service is unreachable — no keyword-only answer, no cached prior answer, no
 * partial result dressed as a complete one — and this panel is the other half of
 * that guarantee: there is no branch here that renders `answer`, because the
 * response shape it takes has no `answer` field to render. Passing one would be a
 * type error, which is the strongest kind of "cannot happen".
 *
 * **It names the request id and the code.** Both are in the error envelope and
 * both are what a report needs: the id joins to a log line and to the
 * `query_logs` row (whose primary key *is* that id), and the code says which
 * dependency failed. A message without them is a dead end.
 *
 * **It says plainly that nothing was substituted.** An operator who sees a
 * failure and no answer needs to know the absence is a decision, not a bug that
 * ate the response.
 */
export interface UnavailablePanelProps {
  error: unknown;
}

export function UnavailablePanel({ error }: UnavailablePanelProps) {
  const code = isApiError(error) ? String(error.code) : "INTERNAL_ERROR";
  const requestId = isApiError(error) ? error.requestId : null;

  return (
    <Card labelledBy="unavailable-heading">
      <CardHeader>
        <CardTitle id="unavailable-heading">The question could not be completed</CardTitle>
        <p className="text-sm">{describeError(error)}</p>
      </CardHeader>
      <CardContent>
        <p className="text-xs text-muted-foreground">
          {`Code: ${code}`}
          <span aria-hidden="true"> · </span>
          {requestId === null ? "No request id was returned." : `Request ${requestId}`}
        </p>
        <p className="mt-2 text-xs text-muted-foreground">
          No answer was returned, and nothing weaker was substituted for one: a degraded search
          would produce a plausible answer that reads exactly like a grounded one.
        </p>
      </CardContent>
    </Card>
  );
}
