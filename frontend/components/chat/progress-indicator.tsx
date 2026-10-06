/**
 * The stage line during a long answer (T132 / FR-035).
 *
 * Why this names the stage: retrieval and reranking are the slow parts, and the
 * wait is not the problem — an invisible wait is. The line comes from the stream
 * itself, not a timer, so a question that is stuck in retrieval says "searching"
 * rather than "writing": the frame is the evidence of where the work is.
 *
 * If the stream produces nothing for a stage, the indicator simply keeps saying
 * the last true thing. That absence is information too, and the indicator does
 * not fake a new one.
 */
export function ProgressIndicator({ stage }: { stage: string | null }) {
  if (stage === null) {
    return null;
  }
  return <p className="text-sm text-muted-foreground">{stage}</p>;
}
