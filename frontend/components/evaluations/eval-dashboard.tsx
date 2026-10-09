"use client";

/**
 * The evaluation dashboard (T149): measured quality, grouped by what it
 * measures, each number attributed to the run that produced it.
 *
 * **Three groups, because "a number" is not a claim.** Retrieval (recall,
 * precision, MRR), answer quality (citation validity, claim coverage,
 * faithfulness, the two zero-count gates), and performance (latency, error
 * rate). A dashboard that listed them as one list would let a latency figure
 * sit beside a correctness figure with equal weight.
 *
 * **Every metric cell renders three things or none.** A label, a value, and the
 * run id + timestamp that measured it. A cell whose value is null renders "not
 * measured" — never `0`, never a dash that could be misread as a perfect
 * score. FR-036/Principle VI: nothing on this page is a hard-coded or estimated
 * number.
 *
 * **The gate banner is not decoration.** A run that fails a gate renders the
 * banner above the metrics, in the fail voice (see `gate-banner.tsx`).
 */
import {
  useEvaluationResults,
  useEvaluationRun,
  useEvaluationRuns,
  useStartEvaluationRun,
  isRunActive,
} from "@/hooks/useEvaluations";
import { EmptyState } from "@/components/evaluations/empty-state";
import { GateBanner } from "@/components/evaluations/gate-banner";
import type { EvaluationMetrics, EvaluationRun } from "@/lib/api-client";

type MetricKey = keyof EvaluationMetrics;

const RETRIEVAL_KEYS: MetricKey[] = [
  "recall_at_5",
  "precision_at_5",
  "mrr",
  "cross_product_domain_coverage",
];
const ANSWER_KEYS: MetricKey[] = [
  "citation_validity",
  "claim_coverage",
  "faithfulness",
  "unsupported_refusal_rate",
  "fabricated_fact_count",
  "invalid_citation_count",
];
const PERFORMANCE_KEYS: MetricKey[] = ["p50_latency_ms", "p95_latency_ms", "error_rate"];

function label(key: MetricKey): string {
  return key.replace(/_/g, " ");
}

function format(key: MetricKey, value: number | null | undefined): string {
  if (value === null || value === undefined) {
    return "not measured";
  }
  if (key.endsWith("_ms")) {
    return `${Math.round(value)} ms`;
  }
  if (key.endsWith("_count")) {
    return String(value);
  }
  return value.toFixed(3);
}

function MetricGroup({
  title,
  run,
  keys,
}: {
  title: string;
  run: EvaluationRun;
  keys: MetricKey[];
}) {
  const metrics = run.metrics;
  return (
    <section aria-labelledby={`group-${title}`} className="grid gap-2">
      <h3 id={`group-${title}`} className="text-sm font-semibold">
        {title}
      </h3>
      <dl className="grid gap-2 sm:grid-cols-2">
        {keys.map((key) => (
          <div key={key} className="rounded-md border px-3 py-2">
            <dt className="text-xs text-muted-foreground">{label(key)}</dt>
            <dd
              className={
                metrics?.[key] === null || metrics?.[key] === undefined
                  ? "text-sm italic text-muted-foreground"
                  : "text-sm font-mono"
              }
            >
              {format(key, metrics?.[key])}
              {/* The attribution line: which run produced this number. */}
              <span className="block text-[11px] text-muted-foreground">
                run {run.id.slice(0, 8)} ·{" "}
                {run.finished_at ?? run.started_at ?? "not started"}
              </span>
            </dd>
          </div>
        ))}
      </dl>
    </section>
  );
}

function RunDetail({ run }: { run: EvaluationRun }) {
  const results = useEvaluationResults(run.id);
  return (
    <div className="grid gap-6">
      <GateBanner outcome={run.gate_outcome} failures={run.gate_failures} />
      <MetricGroup title="Retrieval" run={run} keys={RETRIEVAL_KEYS} />
      <MetricGroup title="Answer quality" run={run} keys={ANSWER_KEYS} />
      <MetricGroup title="Performance" run={run} keys={PERFORMANCE_KEYS} />

      <section aria-labelledby="run-detail-heading" className="grid gap-2">
        <h3 id="run-detail-heading" className="text-sm font-semibold">
          Run
        </h3>
        <dl className="grid gap-1 text-xs text-muted-foreground">
          <div>dataset: {run.dataset_version}</div>
          <div>fingerprint: <span className="font-mono">{run.dataset_fingerprint.slice(0, 16)}…</span></div>
          <div>questions: {run.question_count}</div>
          <div>status: {run.status}</div>
        </dl>
      </section>

      {results.data !== undefined && results.data.total > 0 ? (
        <section aria-labelledby="per-question-heading" className="grid gap-2">
          <h3 id="per-question-heading" className="text-sm font-semibold">
            Per-question results ({results.data.total})
          </h3>
          <ul className="grid gap-1 text-xs">
            {results.data.items.map((r) => (
              <li key={r.question_id} className="flex flex-wrap gap-x-3 rounded-sm border px-2 py-1">
                <span className="font-mono">{r.question_id}</span>
                <span className="text-muted-foreground">{r.refused ? "refused" : "answered"}</span>
                <span className="text-muted-foreground">
                  fabricated: {r.fabricated_fact_count} · invalid citations: {r.invalid_citation_count}
                </span>
                <span className="text-muted-foreground">
                  latency: {r.latency_ms === null ? "not measured" : `${r.latency_ms} ms`}
                </span>
                {r.error !== null ? <span className="text-red-700">{r.error}</span> : null}
              </li>
            ))}
          </ul>
        </section>
      ) : null}
    </div>
  );
}

export function EvalDashboard() {
  const runs = useEvaluationRuns();
  const start = useStartEvaluationRun();
  const latest = runs.data?.items[0] ?? null;
  const detail = useEvaluationRun(latest?.id ?? null);

  if (runs.isError) {
    return <p className="text-sm text-red-700">The evaluation runs could not be loaded.</p>;
  }

  if (runs.isPending) {
    return <p className="text-sm text-muted-foreground">Loading evaluation runs…</p>;
  }

  const run = detail.data ?? latest;

  return (
    <div className="grid gap-6">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h2 className="text-lg font-semibold">Quality metrics</h2>
        <button
          type="button"
          onClick={() => start.mutate()}
          disabled={start.isPending || (run !== null && isRunActive(run.status))}
          className="rounded-md border px-3 py-1 text-sm disabled:opacity-60"
        >
          {run !== null && isRunActive(run.status) ? "Running…" : "Run the suite"}
        </button>
      </div>

      {start.isError ? (
        <p role="alert" className="text-sm text-red-700">
          The run could not be started: {start.error.message}
        </p>
      ) : null}

      {run === null ? (
        <EmptyState onRun={() => start.mutate()} running={start.isPending} />
      ) : isRunActive(run.status) ? (
        <p className="text-sm text-muted-foreground">
          Run in progress ({run.status}). Metrics appear when the suite finishes.
        </p>
      ) : (
        <RunDetail run={run} />
      )}

      {runs.data !== undefined && runs.data.total > 1 ? (
        <section aria-labelledby="run-history-heading" className="grid gap-2">
          <h3 id="run-history-heading" className="text-sm font-semibold">
            Previous runs ({runs.data.total})
          </h3>
          <ul className="grid gap-1 text-xs text-muted-foreground">
            {runs.data.items.slice(1).map((r) => (
              <li key={r.id} className="font-mono">
                {r.id.slice(0, 8)} · {r.gate_outcome} · {r.finished_at ?? "unfinished"}
              </li>
            ))}
          </ul>
        </section>
      ) : null}
    </div>
  );
}