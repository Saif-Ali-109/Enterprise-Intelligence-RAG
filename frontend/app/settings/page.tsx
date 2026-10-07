"use client";

/**
 * The operational settings view (T160, T161): effective configuration, corpus
 * counts, and dependency health in one place.
 *
 * **Secrets are shown as presence only** (FR-042) and **reasoning is shown as
 * `false` because it is a claim an operator can check** (FR-034). Everything
 * else on this page is either counted from live rows or read from the running
 * configuration — nothing is hard-coded here.
 */
import Link from "next/link";
import { ConfigView } from "@/components/corpus/config-view";
import { HealthView } from "@/components/corpus/health-view";

export default function SettingsPage() {
  return (
    <div className="grid gap-8">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h1 className="text-2xl font-semibold">Settings</h1>
        <nav className="flex gap-4 text-sm">
          <Link href="/" className="underline underline-offset-4 hover:no-underline">
            Ask a question
          </Link>
          <Link href="/sources" className="underline underline-offset-4 hover:no-underline">
            Sources
          </Link>
          <Link href="/documents" className="underline underline-offset-4 hover:no-underline">
            Documents
          </Link>
        </nav>
      </div>

      <section aria-labelledby="health-heading" className="grid gap-3">
        <h2 id="health-heading" className="text-lg font-semibold">
          Health
        </h2>
        <HealthView />
      </section>

      <section aria-labelledby="config-heading" className="grid gap-3">
        <h2 id="config-heading" className="text-lg font-semibold">
          Configuration
        </h2>
        <ConfigView />
      </section>
    </div>
  );
}