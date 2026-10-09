"use client";

/**
 * The sources console (T155, T159): register, inspect, crawl, and read the
 * history of every crawl that has run.
 *
 * **The question box is not on this page.** Registering a source and asking a
 * question are different jobs with different failure modes; the corpus is
 * configured here and consulted on the home page.
 */
import Link from "next/link";
import { SourceForm } from "@/components/sources/source-form";
import { SourceTable } from "@/components/corpus/source-table";
import { CrawlHistory } from "@/components/corpus/crawl-history";

export default function SourcesPage() {
  return (
    <div className="grid gap-8">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h1 className="text-2xl font-semibold">Sources</h1>
        <nav className="flex gap-4 text-sm">
          <Link href="/" className="underline underline-offset-4 hover:no-underline">
            Ask a question
          </Link>
          <Link href="/documents" className="underline underline-offset-4 hover:no-underline">
            Documents
          </Link>
          <Link href="/settings" className="underline underline-offset-4 hover:no-underline">
            Settings
          </Link>
        </nav>
      </div>
      <p className="max-w-prose text-sm text-muted-foreground">
        Each source is one Atlassian documentation section. Only these pages are searched, and
        only these pages can support an answer. Page counts are live: a page removed upstream keeps
        its row here as a tombstone rather than disappearing.
      </p>
      <SourceForm />
      <section aria-labelledby="sources-heading" className="grid gap-3">
        <h2 id="sources-heading" className="text-lg font-semibold">
          Registered sources
        </h2>
        <SourceTable />
      </section>
      <section aria-labelledby="crawl-history-heading" className="grid gap-3">
        <h2 id="crawl-history-heading" className="text-lg font-semibold">
          Crawl history
        </h2>
        <CrawlHistory />
      </section>
    </div>
  );
}