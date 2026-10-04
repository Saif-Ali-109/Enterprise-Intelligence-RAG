import type { ReactNode } from "react";
import type { Citation } from "@/lib/api-client";
import { Badge, type BadgeTone } from "@/components/ui/card";

/**
 * The source a citation points at (T093).
 *
 * **Title, product, category, and the full heading path, in that order of
 * prominence.** The heading path is the part that is easy to lose and the part
 * that decides whether a citation is *checkable*: `Boards > Configuration >
 * Permissions` points at the passage, while a collapsed `Boards` points at the
 * page and lets the reader assume a precision the system did not have (FR-018).
 *
 * **The link opens the publisher's page, and says which publisher it is.** The
 * anchor carries `rel="noopener noreferrer"` because the target is a third party
 * (FR-050) — this system is not Atlassian and must not present a link as its own.
 * The hostname is rendered next to the title for the same reason: a reader who
 * lands somewhere should be able to see where they are going first.
 *
 * **The scores are shown as two numbers or as none.** `retrieval_score` is a
 * cosine similarity and `rerank_score` is a relevance score from a different
 * model on a different scale (FR-021); averaging them into one "87% relevant"
 * would be a fabricated statistic. They are rendered separately, and a `null`
 * rerank score — the reranker fell back, or did not run — is reported as absent
 * rather than as zero.
 */

const STATE_TONE: Record<Citation["validation_state"], BadgeTone> = {
  valid: "success",
  repaired: "info",
  stripped: "warning",
};

const STATE_WORD: Record<Citation["validation_state"], string> = {
  valid: "validated",
  repaired: "repaired",
  stripped: "stripped",
};

/** `developer.atlassian.com` — the publisher, without a scheme or a path. */
function publisherOf(url: string): string {
  try {
    return new URL(url).hostname;
  } catch {
    // A malformed link should still render its title. Returning the raw string
    // is more useful than an empty chip, and the anchor below is still a link.
    return url;
  }
}

/** `A > B > C`, or an explicit note when the unit sits above the first heading. */
export function headingPathOf(path: string[]): string {
  if (path.length === 0) {
    return "Top of page";
  }
  return path.join(" › ");
}

export interface SourceCardProps {
  citation: Citation;
  /** Index used in the surrounding label, so the two stay in step. */
  index: number;
  /** Extra content below the link — the quote, in the chat panel. */
  children?: ReactNode;
}

export function SourceCard({ citation, index, children }: SourceCardProps) {
  const repaired = citation.validation_state === "repaired";

  return (
    <article
      className="rounded-md border bg-card p-4 text-sm"
      aria-label={`Source ${index}: ${citation.title}`}
    >
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-mono text-xs text-muted-foreground" aria-hidden="true">
          [{index}]
        </span>
        <Badge tone={STATE_TONE[citation.validation_state]}>
          {STATE_WORD[citation.validation_state]}
        </Badge>
        {citation.product !== null ? <Badge tone="neutral">{citation.product}</Badge> : null}
        {citation.category !== null ? <Badge tone="neutral">{citation.category}</Badge> : null}
      </div>

      <h3 className="mt-2 font-medium">
        <a
          href={citation.source_url}
          target="_blank"
          rel="noopener noreferrer"
          className="underline underline-offset-4 hover:no-underline"
        >
          {citation.title}
        </a>
      </h3>

      <p className="mt-1 text-xs text-muted-foreground">
        <span>{publisherOf(citation.source_url)}</span>
        <span aria-hidden="true"> · </span>
        <span>{headingPathOf(citation.heading_path)}</span>
      </p>

      {/*
        The repair note is rendered whenever a repair happened, and says what the
        repair *was*. A citation whose link the system corrected is a citation
        the operator should be able to distrust on inspection — showing only
        "repaired" would ask for trust the system has not earned back.
      */}
      {repaired ? (
        <p className="mt-2 text-xs text-muted-foreground">
          The model proposed a different link for this source; the link shown was resolved
          from the registry of record instead.
        </p>
      ) : null}

      {children ? <div className="mt-3">{children}</div> : null}

      <p className="mt-3 font-mono text-[11px] text-muted-foreground">
        {/*
          Two scores, never one. `retrieval` is the embedding model's similarity
          and `rerank` is the reranker's; they are different models on different
          scales, and the contract keeps them apart for exactly this reason.
        */}
        {`retrieval ${formatScore(citation.retrieval_score)}`}
        <span aria-hidden="true"> · </span>
        {`rerank ${formatScore(citation.rerank_score)}`}
      </p>
    </article>
  );
}

/**
 * A score, or the word "none".
 *
 * Rounding is presentation only. A score below zero is a legitimate report of
 * dissimilarity (the contract's `-1..1` range is not a mistake), so it is never
 * clamped, and a missing rerank score is never rendered as `0.0`.
 */
function formatScore(score: number | null): string {
  if (score === null) {
    return "none";
  }
  return score.toFixed(3);
}