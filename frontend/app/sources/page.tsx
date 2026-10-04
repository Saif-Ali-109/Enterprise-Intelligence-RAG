"use client";

/**
 * The source console (T091's link target; T155 completes it).
 *
 * **It exists because `app/page.tsx` links to it.** A link to a route that
 * returns 404 is not a navigation affordance, it is a bug that happens to look
 * like a design, so the page is here from the first commit that references it.
 *
 * **The question box is not on this page.** Registering a source and asking a
 * question are different jobs with different failure modes; the corpus is
 * configured here and consulted on the home page.
 */
import Link from "next/link";
import { SourceForm } from "@/components/sources/source-form";
import { SourceList } from "@/components/sources/source-list";

export default function SourcesPage() {
  return (
    <div className="grid gap-8">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h1 className="text-2xl font-semibold">Sources</h1>
        <Link href="/" className="text-sm underline underline-offset-4 hover:no-underline">
          Ask a question
        </Link>
      </div>
      <p className="text-sm text-muted-foreground">
        Each source is one Atlassian documentation section. Only these pages are searched, and
        only these pages can support an answer.
      </p>
      <SourceForm />
      <SourceList />
    </div>
  );
}