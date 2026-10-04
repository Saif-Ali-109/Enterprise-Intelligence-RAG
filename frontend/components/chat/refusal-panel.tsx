import { describeRefusal } from "@/hooks/useChat";
import type { AskResponse } from "@/lib/api-client";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";

/**
 * A refusal, rendered as a refusal (T108 / FR-008).
 *
 * **It is not an error view and does not look like one.** The API answers a
 * refusal with HTTP 200, so a component that branched on `isError` would show
 * every refusal as a fault — the confusion Principle II exists to prevent. This
 * panel has its own region, its own heading, and no error wording anywhere.
 *
 * **It says what was searched, because a refusal you cannot act on is a dead
 * end.** The queries, the products considered, and the three counts come
 * straight from `searched`; nothing here recomputes them, so the numbers on
 * screen are the numbers the service reported.
 *
 * **Leads are labelled as leads.** Each one says it is not sufficient for the
 * question. `insufficient` is a literal `true` in the contract precisely so that
 * no renderer can quietly promote a lead into support for a claim, and the label
 * is rendered from that field rather than from a local judgement.
 */
export interface RefusalPanelProps {
  response: AskResponse;
}

export function RefusalPanel({ response }: RefusalPanelProps) {
  const searched = response.searched;

  return (
    <Card labelledBy="refusal-heading">
      <CardHeader>
        <CardTitle id="refusal-heading">Not answered</CardTitle>
        <p className="text-sm">{describeRefusal(response.refusal_reason)}</p>
      </CardHeader>
      <CardContent>
        <p className="text-sm">
          {`Asked: ${response.question}`}
          <span aria-hidden="true"> · </span>
          {`request ${response.request_id}`}
        </p>

        {searched ? (
          <dl className="mt-4 grid gap-2 text-xs">
            <div className="grid gap-0.5">
              <dt className="font-medium">Searched</dt>
              <dd className="text-muted-foreground">{searched.queries.join(" · ")}</dd>
            </div>
            <div className="grid gap-0.5">
              <dt className="font-medium">Products considered</dt>
              <dd className="text-muted-foreground">
                {searched.products.filter((product): product is string => product !== null).join(", ") ||
                  "none identified"}
              </dd>
            </div>
            <div className="grid gap-0.5">
              <dt className="font-medium">Evidence found</dt>
              <dd className="text-muted-foreground">
                {`${searched.candidates_retrieved} candidates retrieved, ${searched.candidates_reranked} reranked, ${searched.evidence_selected} selected`}
              </dd>
            </div>
          </dl>
        ) : null}

        {response.leads.length > 0 ? (
          <div className="mt-4 grid gap-2">
            <h3 className="text-sm font-medium">Possibly related, not sufficient</h3>
            <ul className="grid gap-2">
              {response.leads.map((lead) => (
                <li key={lead.source_url} className="text-xs">
                  <a
                    href={lead.source_url}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="underline underline-offset-4 hover:no-underline"
                  >
                    {lead.title}
                  </a>
                  {lead.heading_path.length > 0 ? (
                    <span className="ml-2 text-muted-foreground">{lead.heading_path.join(" › ")}</span>
                  ) : null}
                  <span className="ml-2 text-muted-foreground">
                    {lead.insufficient ? "not sufficient for this question" : ""}
                  </span>
                </li>
              ))}
            </ul>
          </div>
        ) : null}

        <p className="mt-4 text-xs text-muted-foreground">
          A refusal is a result, not a failure: nothing was guessed in its place.
        </p>
      </CardContent>
    </Card>
  );
}
