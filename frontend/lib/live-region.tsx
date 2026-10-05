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
  announcement,
  politeness,
  busy,
}: {
  announcement: string | null;
  politeness: "polite" | "assertive" | null;
  busy: boolean;
}) {
  return (
    <div
      aria-live={politeness ?? "polite"}
      aria-busy={busy}
      // Associated With `voiceOff` rather than being a default read, and hidden
      // from visual layout: the region exists for assistive technology.
      className="sr-only"
      role="status"
      aria-label="Progress"
    >
      {announcement ?? ""}
    </div>
  );
}
