"use client";

/**
 * The live region for streamed progress (T130 / FR-060, events.md §8).
 *
 * **Exactly the §8 set, at its politeness.** The function that decides is
 * `announcementFor` in `useAskStream.ts` — one reason to change, and the place a
 * reviewer checks against §8 line by line. This component only renders its
 * output: a visible `polite` region during the work, and an `assertive` one that
 * only ever holds the outcome word.
 *
 * **Instrumentation is not announced.** Classification details, scores, filters
 * and trace internals are inspection detail, not live-region detail. A screen
 * reader that received every counter would need a second screen reader to cope;
 * §8's "announce the ones a human was waiting for" is what keeps it a region
 * rather than a log.
 *
 * **`aria-busy` during the work** toggles the region's busy state while the
 * terminal answer or error has not yet arrived.
 */
export function LiveRegion({
  politeAnnouncement,
  assertiveAnnouncement,
  busy,
}: {
  politeAnnouncement: string | null;
  assertiveAnnouncement: string | null;
  busy: boolean;
}) {
  // Two regions, not one region whose aria-live flips: flipping politeness on a
  // single live region is unreliable in some assistive technology, and the polite
  // history should survive the outcome rather than be overwritten by it.
  return (
    <>
      <div
        aria-live="polite"
        aria-busy={busy}
        className="sr-only"
        role="status"
        aria-label="Progress"
      >
        {politeAnnouncement ?? ""}
      </div>
      <div aria-live="assertive" className="sr-only" role="alert" aria-label="Outcome">
        {assertiveAnnouncement ?? ""}
      </div>
    </>
  );
}
