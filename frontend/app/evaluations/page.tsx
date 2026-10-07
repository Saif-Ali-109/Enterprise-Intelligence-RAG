"use client";

/**
 * The evaluation page (T149): `/evaluations`.
 *
 * **Every number on this page came from a real run.** The dashboard shows no
 * defaults, no estimates, and no placeholder zeroes — before any run has
 * happened it says so in words and offers the button (FR-037).
 */
import Link from "next/link";
import { EvalDashboard } from "@/components/evaluations/eval-dashboard";

export default function EvaluationsPage() {
  return (
    <div className="grid gap-8">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h1 className="text-2xl font-semibold">Evaluation</h1>
        <nav className="flex gap-4 text-sm">
          <Link href="/" className="underline underline-offset-4 hover:no-underline">
            Ask a question
          </Link>
          <Link href="/sources" className="underline underline-offset-4 hover:no-underline">
            Sources
          </Link>
        </nav>
      </div>
      <p className="max-w-prose text-sm text-muted-foreground">
        The gold set runs through the same pipeline that answers your questions — the same
        retrieval, the same evidence gate, the same citation validation. A metric that did not
        move under a real configuration change would be a fabricated metric, and that is the
        tripwire this page exists to make checkable.
      </p>
      <EvalDashboard />
    </div>
  );
}