import { Fragment, type ReactNode } from "react";

/**
 * The `[n]` markers in an answer, rendered as links to their citation (T091).
 *
 * The service resolves the model's `[E3]`-style markers into the citation's rank
 * before the response leaves the backend, so a client never has to know the
 * model's marker vocabulary — only the numbering the citation list is already
 * ordered by.
 *
 * **A marker whose citation is not in the response stays as text.** A link to a
 * citation that is not on the page is a dead link, and silently dropping the
 * number would hide that the answer referenced something the reader cannot see.
 * Leaving `[7]` visible is the honest rendering of a claim whose source did not
 * make it into the list.
 *
 * **Only this exact shape is a marker.** `[1]` and `[12]` are; `[E3]` (the
 * model's own vocabulary, which should have been resolved server-side), `[-1]`,
 * and `[1a]` are prose and are left exactly as written. Regex-matching the answer
 * text is a place where over-matching corrupts content, so the pattern is
 * anchored to digits with no sign and no trailing characters.
 */
const MARKER = /\[(\d{1,2})\]/g;

export function withCitationLinks(answer: string, citationCount: number): ReactNode {
  if (!answer.includes("[")) {
    return answer;
  }

  const parts = answer.split(MARKER);
  return parts.map((part, index) => {
    // Odd indices are the captured ranks; the even ones are the prose between.
    if (index % 2 === 0) {
      return <Fragment key={`t-${index}`}>{part}</Fragment>;
    }
    const rank = Number(part);
    if (!Number.isInteger(rank) || rank < 1 || rank > citationCount) {
      return <Fragment key={`m-${index}`}>{`[${part}]`}</Fragment>;
    }
    return (
      <a
        key={`m-${index}`}
        href={`#citation-${rank}`}
        className="mx-0.5 rounded-sm bg-muted px-1 font-mono text-xs text-muted-foreground underline underline-offset-2 hover:text-foreground"
        aria-label={`Citation ${rank}`}
      >
        {rank}
      </a>
    );
  });
}
