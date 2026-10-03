"use client";

/**
 * The landing page, for now: register a source and watch it crawl (T067, T068).
 *
 * **This page is temporary.** T091 replaces it with the chat interface, and T155
 * moves the source console to `app/sources/page.tsx`. It exists now because
 * "an operator can register a source and see indexed content without writing any
 * code" has to be demonstrable before the retrieval half is built, not after —
 * a crawl that cannot be triggered through the UI is a crawl that only exists in
 * a shell script and a test.
 *
 * **It is a client component because its children are.** Every query hook is one,
 * and the boundary is drawn here rather than around each child: the layout,
 * fonts, and disclaimer stay server-rendered, and the interactive registry is the
 * only thing that hydrates.
 */
import { SourceForm } from "@/components/sources/source-form";
import { SourceList } from "@/components/sources/source-list";

export default function Home() {
  return (
    <div className="grid gap-8">
      <SourceForm />
      <SourceList />
    </div>
  );
}