import type { AskResponse, Citation } from "@/lib/api-client";
import { summariseSources } from "@/components/chat/source-summary";
import { SourceCard } from "@/components/chat/source-card";

/**
 * The multi-domain answer, grouped so its multi-domain nature is visible (T118).
 *
 * **Grouping by product is the whole point.** A flat list of six citations, four
 * of them from Jira and two from Confluence, does not tell a reader that their
 * question spanned two products — it looks like one answer from one place. Grouped,
 * the same six say "this is what Jira says, and this is what Confluence says", and
 * a half-answered question is obvious because one group is thin.
 *
 * **The ambiguity note travels with the groups.** It is the sentence that says
 * which products the question named and that the answer may come from only one of
 * them; rendering the groups without it would leave the reader inferring the
 * cross-domain scope from the grouping alone.
 *
 * **One product renders as one group.** The grouping is a disclosure, not a
 * decoration: a single-product answer gets a heading with its own name and one
 * group, which is what it would look like grouped anyway — no special case, and
 * no different visual weight for "only one domain here".
 */
export interface CrossProductSummaryProps {
  response: AskResponse;
}

export function CrossProductSummary({ response }: CrossProductSummaryProps) {
  const summary = summariseSources(response.citations);
  const groups = groupByProduct(response.citations);

  if (summary.productCount <= 1) {
    return (
      <div className="grid gap-3">
        <h3 className="text-sm font-medium">Sources</h3>
        <CitationList citations={response.citations} />
      </div>
    );
  }

  return (
    <div className="grid gap-4">
      {response.query_analysis?.ambiguity_note ? (
        <p className="text-xs text-muted-foreground">{response.query_analysis.ambiguity_note}</p>
      ) : null}
      {groups.map((group) => (
        <section key={group.product} aria-labelledby={`group-${group.product}`} className="grid gap-2">
          <h4 id={`group-${group.product}`} className="text-sm font-medium">
            {group.product}
            <span className="ml-2 font-normal text-muted-foreground">
              {`${group.citations.length} ${group.citations.length === 1 ? "source" : "sources"}`}
            </span>
          </h4>
          <CitationList citations={group.citations} />
        </section>
      ))}
    </div>
  );
}

/**
 * Citations grouped by product, products in sorted order, citations in rank order.
 *
 * A citation with no product is grouped under `unclassified` rather than dropped:
 * a source the pipeline could not attribute to a product is still a source the
 * reader was shown, and hiding it would make the grouping look tidier than the
 * evidence is.
 */
function groupByProduct(citations: Citation[]): { product: string; citations: Citation[] }[] {
  const groups = new Map<string, Citation[]>();
  for (const citation of citations) {
    const key = citation.product ?? "unclassified";
    const existing = groups.get(key);
    if (existing) {
      existing.push(citation);
    } else {
      groups.set(key, [citation]);
    }
  }
  return [...groups.entries()]
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([product, groupCitations]) => ({
      product,
      citations: [...groupCitations].sort((a, b) => a.rank - b.rank),
    }));
}

function CitationList({ citations }: { citations: Citation[] }) {
  return (
    <ol className="grid gap-3" aria-label="Sources for this answer">
      {citations.map((citation) => (
        <li key={citation.unit_id} id={`citation-${citation.rank}`}>
          <SourceCard citation={citation} index={citation.rank}>
            {citation.quote !== "" ? (
              <blockquote className="border-l-2 pl-3 text-xs italic text-muted-foreground">
                {citation.quote}
              </blockquote>
            ) : null}
          </SourceCard>
        </li>
      ))}
    </ol>
  );
}
