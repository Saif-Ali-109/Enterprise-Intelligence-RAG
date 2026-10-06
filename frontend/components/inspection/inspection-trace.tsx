/**
 * The inspection trace: what happened, in order, and nothing else (T131 / FR-033).
 *
 * **It renders the pipeline state, not a reasoning channel.** Every field shown
 * is one an event carried — counts, scores, ids — because inspection mode shows
 * what a sighted reviewer can act on. What it never shows is a `reasoning` key:
 * the events have no such field to put there, and neither does this component.
 * FR-033 is about being useful to a troubleshooter; FR-034 keeps that from
 * becoming useful to anyone.
 *
 * **The eight stages, in order, with the counts.** The reviewer's question is
 * almost always "where did it stop?" — so stages that ran say so with their
 * counts, and a refusal's zero counts are displayed as zero, not omitted. An
 * empty section reads as "this did not happen", which is a different report
 * than a refusal.
 */
import type { StreamFrameRecord } from "@/hooks/useAskStream";

export function InspectionTrace({ frames }: { frames: StreamFrameRecord[] }) {
  if (frames.length === 0) {
    return null;
  }
  return (
    <section aria-labelledby="trace-heading" className="mt-4 grid gap-2">
      <h3 id="trace-heading" className="text-sm font-medium">
        Pipeline trace
      </h3>
      <ol className="grid gap-1 text-xs text-muted-foreground">
        {frames.map((frame, index) => (
          <li key={index} className="grid gap-1 rounded-sm border px-3 py-2">
            <span className="font-mono">{frame.name}</span>
            {typeof frame.data === "object" && frame.data !== null ? (
              <pre className="overflow-x-auto text-[11px] leading-4">
                {JSON.stringify(frame.data, null, 2)}
              </pre>
            ) : (
              <span>{String(frame.data)}</span>
            )}
          </li>
        ))}
      </ol>
    </section>
  );
}
