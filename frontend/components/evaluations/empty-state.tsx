/**
 * The empty state (T151, FR-037): no run has been performed, and this renders
 * no number at all.
 *
 * **The rule this file exists to enforce.** Every "metric" on this page must be
 * a number a real run measured. An empty state that showed `0%` recall, `—`
 * placeholder rows, or "N/A" in a metric cell would be displaying a fabricated
 * quality value (Principle VI, FR-037). So the empty state renders an
 * explanation and a way to run the suite, and renders *nothing* that looks like
 * a quality measurement — not a zero, not a dash, not a skeleton row.
 *
 * **It names what a run would do**, because "nothing here" with no next step is
 * how a metrics page stays empty forever.
 */
export function EmptyState({ onRun, running }: { onRun: () => void; running: boolean }) {
  return (
    <section aria-labelledby="eval-empty-heading" className="grid gap-4">
      <h2 id="eval-empty-heading" className="text-lg font-semibold">
        No evaluation has been run
      </h2>
      <p className="max-w-prose text-sm text-muted-foreground">
        Quality metrics appear only after the evaluation suite runs against the indexed corpus and
        every question in the gold set has been executed through the live chat pipeline. Until
        then there is nothing to display — no estimate, no default, no placeholder.
      </p>
      <div>
        <button
          type="button"
          onClick={onRun}
          disabled={running}
          className="rounded-md bg-foreground px-4 py-2 text-sm text-background disabled:opacity-60"
        >
          {running ? "Starting…" : "Run the evaluation suite"}
        </button>
      </div>
    </section>
  );
}