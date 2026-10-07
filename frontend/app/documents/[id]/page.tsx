"use client";

/**
 * The document detail route (T157, T158).
 *
 * `use(params)` rather than a client `useParams` so the route resolves the id
 * the same way in both the server-rendered shell and the hydrated view — a
 * detail page that 404s on first paint and then resolves is a page that
 * flickers through an error state.
 */
import Link from "next/link";
import { use } from "react";
import { DocumentDetail } from "@/components/corpus/document-detail";

export default function DocumentPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  return (
    <div className="grid gap-8">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h1 className="text-2xl font-semibold">Document</h1>
        <nav className="flex gap-4 text-sm">
          <Link href="/documents" className="underline underline-offset-4 hover:no-underline">
            All documents
          </Link>
          <Link href="/" className="underline underline-offset-4 hover:no-underline">
            Ask a question
          </Link>
        </nav>
      </div>
      <DocumentDetail documentId={id} />
    </div>
  );
}