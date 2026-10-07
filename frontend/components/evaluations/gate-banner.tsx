/**
 * The gate banner (T150): a failing gate, in plain words, unsoftened.
 *
 * **A fail is never a colour choice.** The banner names each failing gate, its
 * target, and the number the run actually measured. A dashboard that renders a
 * fail as a yellow tint has already failed FR-041 — the failure has to survive
 * contact with a reader who wants it to be a pass. There is no "mostly passing"
 * state here, because the contract's `gate_outcome` is a `pass` or a `fail` and
 * nothing in between is a fact this system measures.
 *
 * **Prose, not a status code.** "recall_at_5: 0.41, target 0.80" is the record;
 * the sentence above it is what a reader needs first. Both are shown, because a
 * reviewer needs to trust the number as well as read the verdict.
 */
import type { EvaluationGateFailure } from "@/lib/api-client";

function humanise(gate: string): string {
  const words = gate.replace(/_/g, " ").trim();
  return words.charAt(0).toUpperCase() + words.slice(1);
}

export function GateBanner({
  outcome,
  failures,
}: {
  outcome: "pass" | "fail";
  failures: EvaluationGateFailure[];
}) {
  if (outcome === "pass") {
    return (
      <div
        role="status"
        aria-label="Quality gate"
        className="rounded-md border border-green-700 bg-green-50 px-4 py-3 text-sm"
      >
        <span className="font-semibold">Gate: pass.</span>{" "}
        <span className="text-muted-foreground">
          Every threshold in force for this run was met.
        </span>
      </div>
    );
  }

  return (
    <div
      role="alert"
      aria-label="Quality gate"
      className="rounded-md border-2 border-red-700 bg-red-50 px-4 py-3 text-sm"
    >
      <p className="font-semibold">
        Gate: fail. {failures.length} {failures.length === 1 ? "gate" : "gates"} outside the
        thresholds this run was judged against.
      </p>
      <ul className="mt-2 grid gap-1">
        {failures.map((f) => (
          <li key={f.gate}>
            <span className="font-medium">{humanise(f.gate)}:</span>{" "}
            <span className="font-mono">{f.actual}</span> against a target of{" "}
            <span className="font-mono">{f.target}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}