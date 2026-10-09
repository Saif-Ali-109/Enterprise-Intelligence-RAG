"use client";

/**
 * The health view (T161): provider, vector service, and database, reported
 * independently.
 *
 * **Three dependencies, three verdicts, one overall status.** FR-060 wants a
 * vector outage to be legible as a vector outage. A single "unhealthy" row
 * would send an operator to the wrong service; the overall status is reported
 * too, but it is the summary of three measurements rather than a measurement of
 * its own.
 *
 * **Latency is shown when it was measured and "not measured" when it was not.**
 * A dependency that failed before a probe could time out has no latency to
 * report, and a `0` there would read as "instantly unreachable".
 *
 * **No probe runs from the browser.** This reads `/health`, which runs its own
 * probes server-side; the view does not re-time the providers from the client,
 * because a browser measuring a cross-network provider would report the
 * browser's latency and call it the service's.
 */
import { useQuery } from "@tanstack/react-query";
import { describeError, system, type DependencyState } from "@/lib/api-client";

function Verdict({ state }: { state: DependencyState }) {
  const className =
    state === "ok"
      ? "text-green-800"
      : state === "degraded"
        ? "text-amber-800"
        : "text-red-700";
  return <span className={className}>{state}</span>;
}

export function HealthView() {
  const query = useQuery({
    queryKey: ["health"],
    queryFn: ({ signal }) => system.health(signal),
    refetchInterval: 15_000,
  });

  if (query.isPending) {
    return <p className="text-sm text-muted-foreground">Checking dependencies…</p>;
  }
  if (query.isError) {
    return (
      <p role="alert" className="text-sm text-red-700">
        {describeError(query.error)}
      </p>
    );
  }

  const health = query.data;

  return (
    <div className="grid gap-4">
      <p className="text-sm">
        Overall status: <Verdict state={health.status} />
        {health.version !== null ? (
          <span className="text-muted-foreground"> · version {health.version}</span>
        ) : null}
      </p>

      <dl className="grid gap-2">
        {Object.entries(health.dependencies).map(([name, report]) => (
          <div key={name} className="grid gap-1 rounded-md border px-3 py-2">
            <dt className="text-xs text-muted-foreground">{name}</dt>
            <dd className="flex flex-wrap items-baseline gap-3 text-sm">
              <Verdict state={report.status} />
              <span className="text-muted-foreground">
                latency:{" "}
                {report.latency_ms === null ? "not measured" : `${report.latency_ms} ms`}
              </span>
            </dd>
            {report.detail !== null ? (
              <p className="text-xs text-muted-foreground">{report.detail}</p>
            ) : null}
          </div>
        ))}
      </dl>
    </div>
  );
}