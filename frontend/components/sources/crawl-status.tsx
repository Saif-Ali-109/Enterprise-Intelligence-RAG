"use client";

/**
 * Live crawl state (T068), and the concurrent-crawl refusal.
 *
 * **Polling, not streaming.** The API reports crawl progress by reading a row,
 * so there is nothing to stream; `useCrawlJob` polls every two seconds while the
 * job is queued or running and stops the moment it is not. A crawl that finished
 * an hour ago costs no requests, which is the only way polling is acceptable in
 * a tool that may sit open all day.
 *
 * **Six states, six renderings.** `completed_with_errors` is not folded into
 * `completed` and not folded into `failed`: a crawl that indexed forty pages and
 * failed two is the normal outcome of a documentation site that moved a page,
 * and reading it as either extreme is a lie told to the operator (FR-054). The
 * counters and the per-reason skip map are shown for every terminal state,
 * because "it finished" is not the question; "what happened to the pages" is.
 *
 * **The live region announces the state and nothing else.** Counters change on
 * every poll; announcing them would interrupt a screen-reader user roughly every
 * two seconds for the length of a crawl. The status word changes a handful of
 * times, and it is what the operator is actually waiting for. This is the same
 * rule `contracts/events.md` §8 states for the answer stream: progress a human
 * asked about is announced, instrumentation is not.
 *
 * **A refusal is shown as a refusal.** `CRAWL_ALREADY_RUNNING` names the job
 * that holds the slot, and the view links to it — the alternative, silently
 * doing nothing, is indistinguishable from a broken button.
 */
import { useId } from "react";
import { ApiError, describeError, type CrawlJob, type CrawlJobStatus } from "@/lib/api-client";
import { isCrawlActive, useCrawlJob } from "@/hooks/useRegistry";
import { Badge, type BadgeTone } from "@/components/ui/card";
import { Button } from "@/components/ui/button";

/** One word per state, plus the tone that matches it. The word is never omitted. */
const STATUS_TONE: Record<CrawlJobStatus, BadgeTone> = {
  queued: "neutral",
  running: "info",
  completed: "success",
  completed_with_errors: "warning",
  failed: "danger",
  cancelled: "neutral",
};

const STATUS_EXPLANATION: Record<CrawlJobStatus, string> = {
  queued: "Accepted. The crawl has not started fetching yet.",
  running: "Fetching pages. The counters below move as each page finishes.",
  completed: "Finished. Every page discovered was processed without a failure.",
  completed_with_errors:
    "Finished with errors. The pages that succeeded are indexed; the failures are listed below.",
  failed: "The crawl failed before or during indexing. Nothing new is retrievable from this run.",
  cancelled: "The service stopped while this crawl was in progress. Trigger it again to retry.",
};

export interface CrawlStatusProps {
  /** The job to watch. `null` renders nothing rather than an empty shell. */
  jobId: string | null;
  /**
   * A refusal to show alongside the job.
   *
   * A rejected crawl has no job to watch — the refusal *is* the result — so this
   * is how `CRAWL_ALREADY_RUNNING` reaches the operator.
   */
  error?: ApiError | null;
  /** Offered when the crawl failed or was cancelled; re-triggering is the fix. */
  onRetry?: () => void;
}

export function CrawlStatus({ jobId, error = null, onRetry }: CrawlStatusProps) {
  const query = useCrawlJob(jobId);

  if (error !== null && isConcurrentRefusal(error)) {
    return <ConcurrentCrawlNotice error={error} />;
  }
  if (jobId === null) {
    return null;
  }
  if (query.isPending) {
    return (
      <section aria-busy="true" className="rounded-lg border p-4 text-sm text-muted-foreground">
        Loading crawl status…
      </section>
    );
  }
  if (query.isError) {
    return (
      <section role="alert" className="rounded-lg border border-destructive/40 bg-destructive/10 p-4 text-sm">
        <p className="font-medium">Crawl status is unavailable</p>
        <p className="mt-1">{describeError(query.error)}</p>
      </section>
    );
  }

  return <JobReport job={query.data} onRetry={onRetry} />;
}

function isConcurrentRefusal(error: ApiError): boolean {
  return error.code === "CRAWL_ALREADY_RUNNING" || error.status === 409;
}

/**
 * `CRAWL_ALREADY_RUNNING`.
 *
 * The running job's id is in `details.crawl_job_id`, so this can point at the
 * crawl that is actually holding the slot instead of only complaining that
 * something does.
 */
function ConcurrentCrawlNotice({ error }: { error: ApiError }) {
  const headingId = useId();
  const jobId = typeof error.details.crawl_job_id === "string" ? error.details.crawl_job_id : null;

  return (
    <section
      role="alert"
      aria-labelledby={headingId}
      className="rounded-lg border border-destructive/40 bg-destructive/10 p-4"
    >
      <h3 id={headingId} className="text-sm font-medium">
        A crawl is already running for this source
      </h3>
      <p className="mt-1 text-sm">{describeError(error)}</p>
      <p className="mt-2 text-sm">
        {jobId === null ? (
          "Wait for it to finish, then trigger this crawl again."
        ) : (
          <>
            Watch the running crawl below, or trigger this one again once it finishes. Running
            job:{" "}
            <code className="rounded bg-muted px-1 py-0.5 text-xs">{jobId}</code>
          </>
        )}
      </p>
    </section>
  );
}

function JobReport({ job, onRetry }: { job: CrawlJob; onRetry: (() => void) | undefined }) {
  const headingId = useId();
  const reasons = Object.entries(job.skipped_reasons ?? {}).sort(([a], [b]) => a.localeCompare(b));

  return (
    <section aria-labelledby={headingId} className="rounded-lg border p-4">
      <div className="flex flex-wrap items-center gap-3">
        <h3 id={headingId} className="text-sm font-medium">
          Crawl {job.id.slice(0, 8)}
        </h3>
        {/* The only live region here: the state word, which changes a few times
            and not the counters, which change every poll. */}
        <span role="status" aria-live="polite">
          <Badge tone={STATUS_TONE[job.status]}>{job.status.replace(/_/g, " ")}</Badge>
        </span>
        {job.error_code !== null ? (
          <span className="text-xs text-muted-foreground">error {job.error_code}</span>
        ) : null}
        {onRetry !== undefined && (job.status === "failed" || job.status === "cancelled") ? (
          <Button size="sm" variant="outline" onClick={onRetry}>
            Crawl again
          </Button>
        ) : null}
      </div>

      <p className="mt-2 text-sm text-muted-foreground">{STATUS_EXPLANATION[job.status]}</p>

      <dl className="mt-4 grid grid-cols-2 gap-3 text-sm sm:grid-cols-5">
        <Counter label="discovered" value={job.counters.discovered} />
        <Counter label="processed" value={job.counters.processed} />
        <Counter label="unchanged" value={job.counters.unchanged} />
        <Counter label="skipped" value={job.counters.skipped} />
        <Counter label="failed" value={job.counters.failed} />
      </dl>

      {reasons.length > 0 ? (
        <div className="mt-4">
          <h4 className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
            Why pages were skipped
          </h4>
          <ul className="mt-1 flex flex-wrap gap-x-4 gap-y-1 text-sm">
            {reasons.map(([reason, count]) => (
              <li key={reason}>
                <span className="font-mono text-xs">{reason}</span>
                <span className="ml-1 text-muted-foreground">
                  {count} {count === 1 ? "page" : "pages"}
                </span>
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      {job.error_message !== null ? (
        <p className="mt-3 text-sm">{job.error_message}</p>
      ) : null}

      <dl className="mt-4 grid gap-1 text-xs text-muted-foreground sm:grid-cols-3">
        <div>
          <dt className="inline font-medium">requested </dt>
          <dd className="inline">{job.requested_at}</dd>
        </div>
        <div>
          <dt className="inline font-medium">started </dt>
          <dd className="inline">{job.started_at ?? "not yet"}</dd>
        </div>
        <div>
          <dt className="inline font-medium">finished </dt>
          <dd className="inline">{job.finished_at ?? (isCrawlActive(job.status) ? "in progress" : "—")}</dd>
        </div>
      </dl>

      <p className="mt-3 text-xs text-muted-foreground">
        <a
          href={`${job.target_url}`}
          target="_blank"
          rel="noopener noreferrer"
          className="underline underline-offset-2"
        >
          Open the page this crawl started from
        </a>
      </p>
    </section>
  );
}

function Counter({ label, value }: { label: string; value: number }) {
  return (
    <div>
      <dt className="text-xs text-muted-foreground">{label}</dt>
      <dd className="text-lg font-medium tabular-nums">{value}</dd>
    </div>
  );
}