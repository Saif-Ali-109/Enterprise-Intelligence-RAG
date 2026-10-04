import type { Citation } from "@/lib/api-client";
import { SourceCard } from "@/components/chat/source-card";

/**
 * The citations under an answer (T092).
 *
 * **Every citation links to the original publisher page with its heading path.**
 * The list is not a bibliography at the bottom of the page — it is the evidence
 * for the text above it, so it is ordered by the same rank the answer uses, and
 * each entry carries the section the passage came from (FR-050, FR-018, SC-018).
 *
 * **`target="_blank"` with `rel="noopener noreferrer"` on every link.** These are
 * third-party pages, opened by a system that is not affiliated with them; the
 * `rel` is what stops the opened page from reaching back into this one.
 *
 * **A repaired citation is labelled, in place.** When the model's proposed link
 * disagreed with the registry, the registry's link is what a reader gets and the
 * entry says so — a silently corrected link would teach an operator to trust
 * links that were wrong.
 *
 * **An empty list renders a sentence, not an empty box.** A response with
 * `outcome: "answered"` and no citations is a defect, and the list says exactly
 * that rather than collapsing to nothing, which would read as "no sources were
 * needed".
 */

export interface CitationListProps {
  citations: Citation[];
  /** Shown when the list is empty. The panel passes the answer's outcome. */
  emptyMessage?: string;
}

export function CitationList({
  citations,
  emptyMessage = "No citation was returned with this answer.",
}: CitationListProps) {
  if (citations.length === 0) {
    return <p className="text-sm text-muted-foreground">{emptyMessage}</p>;
  }

  return (
    <ol className="grid gap-3" aria-label="Sources for this answer">
      {citations.map((citation) => (
        <li key={citation.unit_id}>
          <SourceCard citation={citation} index={citation.rank}>
            {/*
              The supporting span, quoted from the unit the citation names. It is
              Atlassian's text, so it is rendered as a quotation — never as this
              system's own words, and never truncated silently.
            */}
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