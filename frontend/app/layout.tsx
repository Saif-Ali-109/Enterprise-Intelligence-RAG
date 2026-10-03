/**
 * The document shell.
 *
 * **`lang="en"` is load-bearing, not decorative.** A screen reader chooses its
 * pronunciation rules by document language; without it, English content is read
 * with whatever rules the browser guesses, which for technical terms means
 * letter-by-letter nonsense.
 *
 * **No web font.** `next/font/google` downloads at build time, which makes the
 * Docker build depend on a third-party host being reachable and gives a
 * reproducible image one fewer external dependency to fail. The system font stack
 * is used instead, and it is the same on the server and in the browser — so
 * nothing reflows after hydration.
 *
 * **The disclaimer is in the shell, not on one page.** It is required wherever
 * the system is presented (FR-052), and a footer that exists only on the chat
 * page would be missing from the corpus views.
 */
import type { Metadata } from "next";
import type { ReactNode } from "react";
import { Providers } from "@/app/providers";
import "@/app/globals.css";

export const metadata: Metadata = {
  title: "Enterprise Knowledge Intelligence",
  description:
    "Evidence-grounded answers from public Atlassian documentation, with a citation for every claim.",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <body className="min-h-screen font-sans antialiased">
        <Providers>
          <div className="mx-auto flex min-h-screen max-w-6xl flex-col gap-8 px-4 py-8">
            <header className="grid gap-2">
              <h1 className="text-2xl font-semibold tracking-tight">Enterprise Knowledge Intelligence</h1>
              <p className="text-sm text-muted-foreground">
                Answers grounded in publicly available Atlassian documentation, with the source
                section for every claim.
              </p>
            </header>
            <main className="flex-1">{children}</main>
            <footer className="border-t pt-4 text-xs text-muted-foreground">
              <p>
                Not affiliated with, endorsed by, or sponsored by Atlassian. Documentation content
                remains the property of its authors; this tool fetches public pages and cites them.
              </p>
            </footer>
          </div>
        </Providers>
      </body>
    </html>
  );
}