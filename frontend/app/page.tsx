"use client";

/**
 * The landing page (T091): ask a question.
 *
 * **The registry console moved to `/sources`, and this page says so.** Registering
 * a source is real work — URLs, page caps, a crawl to watch — and it does not
 * belong above the question box that every visitor came for (T155 completes the
 * move; until then `/sources` exists and this page links to it).
 *
 * **A client component because the panel is one.** The layout, fonts and
 * disclaimer stay server-rendered; the chat panel hydrates.
 */
import Link from "next/link";
import { ChatPanel } from "@/components/chat/chat-panel";
import { SourceList } from "@/components/sources/source-list";

export default function Home() {
  return (
    <div className="grid gap-8">
      <ChatPanel />
      <section aria-labelledby="corpus-heading" className="grid gap-4">
        <div className="flex flex-wrap items-baseline justify-between gap-2">
          <h2 id="corpus-heading" className="text-lg font-semibold">
            Indexed corpus
          </h2>
          <nav className="flex gap-4 text-sm">
            <Link href="/sources" className="underline underline-offset-4 hover:no-underline">
              Manage sources
            </Link>
            <Link href="/evaluations" className="underline underline-offset-4 hover:no-underline">
              Evaluation
            </Link>
          </nav>
        </div>
        <p className="text-sm text-muted-foreground">
          Answers come only from the pages registered here. Nothing else is searched.
        </p>
        <SourceList />
      </section>
    </div>
  );
}