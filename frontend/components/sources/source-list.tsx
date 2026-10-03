"use client";

/**
 * Registered sources, with their indexed content and their actions (T067).
 *
 * **The counts here are the corpus, stated plainly.** `page_count` and
 * `unit_count` are the only figures that answer "did my crawl do anything", and
 * they come from the API rather than from anything remembered in the browser.
 * A source that has never been crawled reads `0` — that is a fact about the
 * corpus, not a placeholder, and it is never smoothed over with a dash.
 *
 * **Destructive actions ask twice, in place.** Removing a source deletes its
 * documents and its vectors (FR-031), and that cannot be undone by re-crawling:
 * the fingerprints are gone, so the next crawl re-embeds rather than skipping.
 * `window.confirm` is not used because its dialog cannot be styled, is announced
 * inconsistently, and reads the same whether it is confirming a deletion or a
 * navigation. Instead the row's button becomes a two-button choice, and focus
 * moves to the confirming button so a keyboard user is looking at the decision
 * rather than at a row that changed under them.
 *
 * **Removal is blocked while a crawl is live**, with the reason shown rather
 * than the button silently disabled — the backend answers 409 in that case, and
 * an operator who cannot see why a button is dead will press it again.
 *
 * **Nothing here is colour-only** (FR-059): status is a badge with a word, and
 * every action has a text label rather than an icon a screen reader announces as
 * "button".
 */
import { useRef, useState } from "react";
import { describeError, isApiError, type Source } from "@/lib/api-client";
import { formatRelative, formatTimestamp } from "@/lib/utils";
import {
  isCrawlActive,
  useRemoveSource,
  useSources,
  useStartCrawl,
  useUpdateSource,
} from "@/hooks/useRegistry";
import { Badge, Card, CardContent, CardDescription, CardHeader, CardTitle, type BadgeTone } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Table, TableBody, TableCaption, TableCell, TableHead, TableRow } from "@/components/ui/table";
import { CrawlStatus } from "@/components/sources/crawl-status";

const PAGE_SIZE = 10;

const SOURCE_TONE: Record<Source["status"], BadgeTone> = {
  active: "success",
  disabled: "neutral",
  error: "danger",
};

export function SourceList() {
  const [offset, setOffset] = useState(0);
  const query = useSources({ limit: PAGE_SIZE, offset });
  const startCrawl = useStartCrawl();

  // Which job to watch, and which refusal to show. Both are set by an action
  // above the table and cleared by the next one: this panel answers "what
  // happened to the click I just made", not "what is the newest job".
  const [watchedJobId, setWatchedJobId] = useState<string | null>(null);
  const [watchedSourceId, setWatchedSourceId] = useState<string | null>(null);

  function onCrawlRequested(source: Source): void {
    setWatchedJobId(null);
    setWatchedSourceId(source.id);
    startCrawl.mutate(source.id, {
      onSuccess: (job) => setWatchedJobId(job.id),
      // A refusal leaves `watchedJobId` null and puts the error in `error`, which
      // `CrawlStatus` renders as the concurrent-crawl notice.
      onError: () => setWatchedJobId(null),
    });
  }

  return (
    <Card labelledBy="source-list-title">
      <CardHeader>
        <CardTitle id="source-list-title">Registered sources</CardTitle>
        <CardDescription>
          Every source this instance has ingested, with the size of its indexed content. Page text
          is never stored in this application&apos;s database — the units live in the vector store
          and are read back one at a time.
        </CardDescription>
      </CardHeader>
      <CardContent className="grid">
        {query.isPending ? (
          <p className="text-sm text-muted-foreground" aria-busy="true">
            Loading sources…
          </p>
        ) : null}

        {query.isError ? (
          <div role="alert" className="rounded-md border border-destructive/40 bg-destructive/10 p-3 text-sm">
            <p className="font-medium">The source list could not be loaded</p>
            <p className="mt-1">{describeError(query.error)}</p>
          </div>
        ) : null}

        {query.data !== undefined && query.data.items.length === 0 ? (
          <p className="text-sm text-muted-foreground">
            No sources are registered yet. Add one above to start ingesting documentation.
          </p>
        ) : null}

        {query.data !== undefined && query.data.items.length > 0 ? (
          <>
            <Table>
              <TableCaption className="sr-only">
                Registered sources, with the pages and units each has indexed
              </TableCaption>
              <thead>
                <TableRow>
                  <TableHead>Name</TableHead>
                  <TableHead>Product</TableHead>
                  <TableHead className="text-right">Pages</TableHead>
                  <TableHead className="text-right">Units</TableHead>
                  <TableHead>Last crawl</TableHead>
                  <TableHead>Status</TableHead>
                  <TableHead>Actions</TableHead>
                </TableRow>
              </thead>
              <TableBody>
                {query.data.items.map((source) => (
                  <SourceRow
                    key={source.id}
                    source={source}
                    busy={startCrawl.isPending && watchedSourceId === source.id}
                    onCrawl={() => onCrawlRequested(source)}
                  />
                ))}
              </TableBody>
            </Table>

            <Pagination
              total={query.data.total}
              offset={offset}
              onOffset={setOffset}
              disabled={query.isFetching}
            />
          </>
        ) : null}

        <CrawlStatus
          jobId={watchedJobId}
          error={isApiError(startCrawl.error) ? startCrawl.error : null}
        />
      </CardContent>
    </Card>
  );
}

/**
 * One source's row and its actions.
 *
 * The actions are small enough to live in the row, which keeps the decision —
 * "crawl this one" — next to the thing it applies to. Anything that grows past
 * three actions belongs in the source's own view (T155).
 */
function SourceRow({
  source,
  onCrawl,
  busy,
}: {
  source: Source;
  onCrawl: () => void;
  busy: boolean;
}) {
  const update = useUpdateSource();
  const remove = useRemoveSource();
  const [confirmingRemoval, setConfirmingRemoval] = useState(false);
  const confirmRef = useRef<HTMLButtonElement>(null);

  const lastCrawl = formatTimestamp(source.last_crawl_at);
  const lastCrawlRelative = formatRelative(source.last_crawl_at);
  const crawlInProgress = isCrawlActive(source.last_crawl_status);
  // A disabled source cannot be crawled — the API refuses it rather than
  // contradicting the operator's own instruction — so the button is disabled
  // here with the reason attached, not left to fail after a round trip.
  const crawlBlocked = !source.enabled;
  const blockedHintId = `crawl-blocked-${source.id}`;

  function askToRemove(): void {
    setConfirmingRemoval(true);
    // Move focus to the confirming control: a keyboard user must be looking at
    // the decision, and an unannounced change two cells to the left is invisible
    // to them.
    confirmRef.current?.focus();
  }

  function toggleEnabled(): void {
    update.mutate({ id: source.id, patch: { enabled: !source.enabled } });
  }

  function confirmRemove(): void {
    remove.mutate(source.id, {
      onSettled: () => setConfirmingRemoval(false),
    });
  }

  return (
    <TableRow>
      <TableCell className="font-medium">
        <a
          href={source.start_url}
          target="_blank"
          rel="noopener noreferrer"
          className="underline underline-offset-2"
        >
          {source.name}
        </a>
        <span className="block text-xs font-normal text-muted-foreground break-all">
          {source.start_url}
        </span>
      </TableCell>

      <TableCell>
        {/* FR-013: an unset classification is rendered as an absence, never as a
            guess. "Cross-product" would be an assertion this system cannot make. */}
        {source.product_domain ?? <span className="text-muted-foreground">not specified</span>}
      </TableCell>

      <TableCell className="text-right tabular-nums">{source.page_count}</TableCell>
      <TableCell className="text-right tabular-nums">{source.unit_count}</TableCell>

      <TableCell>
        {lastCrawl === null ? (
          <span className="text-muted-foreground">never crawled</span>
        ) : (
          <>
            <time dateTime={source.last_crawl_at ?? undefined} title={lastCrawl}>
              {lastCrawlRelative ?? lastCrawl}
            </time>
            {source.last_crawl_status === null ? null : (
              <span className="block text-xs text-muted-foreground">
                {source.last_crawl_status.replace(/_/g, " ")}
              </span>
            )}
          </>
        )}
      </TableCell>

      <TableCell>
        <Badge tone={SOURCE_TONE[source.status]}>{source.status}</Badge>
      </TableCell>

      <TableCell>
        <div className="flex flex-wrap items-center gap-2">
          <Button
            size="sm"
            onClick={onCrawl}
            disabled={crawlBlocked || busy}
            aria-describedby={crawlBlocked ? blockedHintId : undefined}
          >
            {crawlInProgress ? "Crawling…" : "Crawl now"}
          </Button>
          {crawlBlocked ? (
            <span id={blockedHintId} className="sr-only">
              This source is disabled. Enable it before crawling.
            </span>
          ) : null}

          <Button size="sm" variant="outline" onClick={toggleEnabled} disabled={update.isPending}>
            {source.enabled ? "Disable" : "Enable"}
          </Button>

          {confirmingRemoval ? (
            <>
              <Button
                ref={confirmRef}
                size="sm"
                variant="destructive"
                onClick={confirmRemove}
                disabled={remove.isPending}
              >
                {remove.isPending ? "Removing…" : `Remove ${source.name} and its vectors`}
              </Button>
              <Button
                size="sm"
                variant="ghost"
                onClick={() => setConfirmingRemoval(false)}
                disabled={remove.isPending}
              >
                Keep it
              </Button>
            </>
          ) : (
            <Button size="sm" variant="ghost" onClick={askToRemove} disabled={remove.isPending}>
              Remove
            </Button>
          )}
        </div>

        {remove.isError ? (
          <p role="alert" className="mt-1 text-xs font-medium text-destructive">
            {describeError(remove.error)}
          </p>
        ) : null}
        {update.isError ? (
          <p role="alert" className="mt-1 text-xs font-medium text-destructive">
            {describeError(update.error)}
          </p>
        ) : null}
        {remove.isError && isApiError(remove.error) && remove.error.status === 409 ? (
          <p className="mt-1 text-xs text-muted-foreground">
            A crawl is running for this source. Wait for it to finish, then remove it.
          </p>
        ) : null}
      </TableCell>
    </TableRow>
  );
}

/**
 * Offset pagination.
 *
 * Buttons rather than a number input: the operator's question is "are there more"
 * and "go back", not "what is row 47". The range is stated in words because
 * "11–20 of 155" answers a different question than "page 2".
 */
function Pagination({
  total,
  offset,
  onOffset,
  disabled,
}: {
  total: number;
  offset: number;
  onOffset: (next: number) => void;
  disabled: boolean;
}) {
  const first = total === 0 ? 0 : offset + 1;
  const last = Math.min(offset + PAGE_SIZE, total);
  const hasPrevious = offset > 0;
  const hasNext = last < total;

  if (total <= PAGE_SIZE) {
    return (
      <p className="text-xs text-muted-foreground">
        {total === 0 ? "No sources." : `Showing all ${total}.`}
      </p>
    );
  }

  return (
    <div className="flex items-center gap-3 text-sm">
      <Button
        size="sm"
        variant="outline"
        onClick={() => onOffset(Math.max(0, offset - PAGE_SIZE))}
        disabled={!hasPrevious || disabled}
      >
        Previous
      </Button>
      <p className="text-muted-foreground" aria-live="polite">
        {first}–{last} of {total}
      </p>
      <Button
        size="sm"
        variant="outline"
        onClick={() => onOffset(offset + PAGE_SIZE)}
        disabled={!hasNext || disabled}
      >
        Next
      </Button>
    </div>
  );
}