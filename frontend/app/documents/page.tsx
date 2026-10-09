"use client";

/**
 * The documents browser (T156): the indexed corpus, filterable, with a link
 * back to every page's origin.
 */
import Link from "next/link";
import { DocumentTable } from "@/components/corpus/document-table";

export default function DocumentsPage() {
  return (
    <div className="grid gap-8">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h1 className="text-2xl font-semibold">Documents</h1>
        <nav className="flex gap-4 text-sm">
          <Link href="/" className="underline underline-offset-4 hover:no-underline">
            Ask a question
          </Link>
          <Link href="/sources" className="underline underline-offset-4 hover:no-underline">
            Sources
          </Link>
          <Link href="/settings" className="underline underline-offset-4 hover:no-underline">
            Settings
          </Link>
        </nav>
      </div>
      <p className="max-w-prose text-sm text-muted-foreground">
        Every page this instance has ingested, with its provenance and lifecycle state. Page text is
        never stored here — the chunks live in the vector index and are read back one at a time when
        a question needs them.
      </p>
      <DocumentTable />
    </div>
  );
}