"use client";

/**
 * Crawl history (T159): what was requested, what was read, and what failed.
 *
 * **The counters answer the question the operator actually has** — "why did
 * this crawl index fewer pages than it found?" — so they are shown side by side
 * rather than summed into one total: discovered, processed, unchanged,
 * skipped, failed. A total would hide the difference between "we read it and it
 * had not changed" (fine) and "we did not read it" (a problem).
 *
 * **`skipped_reasons` is a breakdown, and it is null when nothing was skipped.**
 * An empty map and a missing breakdown are different reports: one says "nothing
 * was skipped", the other says "this job never reported a breakdown".
 *
 * **An error is shown with its message.** A failed job that renders as a status
 * pill alone sends the reader to the logs; the message is already in the record.
 */
import { useCrawlJobs } from "@/hooks/useRegistry";
import { describeError } from "@/lib/api-client";
import {
  Table,
  TableBody,
  TableCaption,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";

const PAGE_SIZE = 20;

function stamp(value: string | null): string {
  return value === null ? "—" : new Date(value).toISOString().slice(0, 16).replace("T", " ");
}

export function CrawlHistory({ sourceId }: { sourceId?: string } = {}) {
  const query = useCrawlJobs({
    limit: PAGE_SIZE,
    ...(sourceId === undefined ? {} : { source_id: sourceId }),
  });

  if (query.isPending) {
    return <p className="text-sm text-muted-foreground">Loading crawl history…</p>;
  }
  if (query.isError) {
    return (
      <p role="alert" className="text-sm text-red-700">
        {describeError(query.error)}
      </p>
    );
  }

  const { items } = query.data;

  return (
    <Table>
      <TableCaption className="sr-only">
        Crawl jobs with pages discovered, processed, unchanged, skipped and failed, and any error
        detail.
      </TableCaption>
      <TableHeader>
        <TableRow>
          <TableHead scope="col">Requested</TableHead>
          <TableHead scope="col">Status</TableHead>
          <TableHead scope="col">Discovered</TableHead>
          <TableHead scope="col">Processed</TableHead>
          <TableHead scope="col">Unchanged</TableHead>
          <TableHead scope="col">Skipped</TableHead>
          <TableHead scope="col">Failed</TableHead>
          <TableHead scope="col">Detail</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {items.length === 0 ? (
          <TableRow>
            <TableCell colSpan={8} className="text-muted-foreground">
              No crawl has been requested.
            </TableCell>
          </TableRow>
        ) : (
          items.map((job) => (
            <TableRow key={job.id}>
              <TableCell className="font-mono text-xs">{stamp(job.requested_at)}</TableCell>
              <TableCell
                className={
                  job.status === "failed" || job.status === "cancelled"
                    ? "text-red-700"
                    : job.status === "completed_with_errors"
                      ? "text-amber-800"
                      : undefined
                }
              >
                {job.status}
              </TableCell>
              <TableCell className="font-mono">{job.counters.discovered}</TableCell>
              <TableCell className="font-mono">{job.counters.processed}</TableCell>
              <TableCell className="font-mono">{job.counters.unchanged}</TableCell>
              <TableCell className="font-mono">{job.counters.skipped}</TableCell>
              <TableCell className="font-mono">{job.counters.failed}</TableCell>
              <TableCell className="text-xs">
                {job.error_message !== null ? (
                  <span className="block text-red-700">{job.error_message}</span>
                ) : null}
                {job.skipped_reasons === null ? null : (
                  <span className="block text-muted-foreground">
                    {Object.entries(job.skipped_reasons)
                      .map(([reason, count]) => `${reason}: ${count}`)
                      .join(", ")}
                  </span>
                )}
                {job.error_message === null && job.skipped_reasons === null ? (
                  <span className="text-muted-foreground">—</span>
                ) : null}
              </TableCell>
            </TableRow>
          ))
        )}
      </TableBody>
    </Table>
  );
}