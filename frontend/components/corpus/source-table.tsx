"use client";

/**
 * The sources console (T155): identity, product, pages indexed, last crawl, and
 * the per-source actions.
 *
 * **Live counts, not remembered ones.** `page_count` and `unit_count` come
 * from the API's counted rows with tombstones excluded, so the table reports
 * what is searchable now — the distinction matters most right after a deletion,
 * when the two numbers disagree.
 *
 * **A source with zero pages is still a row.** Dropping a source from the table
 * the moment its last document is deleted would hide a registration the operator
 * is still crawling from. Zero is a fact about the corpus, not a reason to stop
 * showing the thing (edge case 16, asserted by `test_corpus_views.py`).
 *
 * **"Never crawled" and "the last crawl failed" are different cells.** The
 * status column says which one it is, because an empty crawl history reads as
 * either depending on who is looking.
 *
 * **Destructive actions ask first.** Removing a source discards its registered
 * pages from this application's index, and an accidental click should not be
 * the only undo there is.
 */
import { useState } from "react";
import Link from "next/link";
import {
  useRemoveSource,
  useSources,
  useStartCrawl,
  useUpdateSource,
} from "@/hooks/useRegistry";
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

const PAGE_SIZE = 25;

/** Status text for the last crawl, kept separate so null reads as its own state. */
function LastCrawl({
  at,
  status,
}: {
  at: string | null;
  status: string | null;
}) {
  if (at === null) {
    return <span className="text-muted-foreground">never crawled</span>;
  }
  const stamp = new Date(at).toISOString().slice(0, 16).replace("T", " ");
  return (
    <span className="grid gap-0.5">
      <span className="font-mono">{stamp}</span>
      <span
        className={
          status === "failed" || status === "cancelled" ? "text-red-700" : "text-muted-foreground"
        }
      >
        {status ?? "unknown"}
      </span>
    </span>
  );
}

export function SourceTable() {
  const [offset, setOffset] = useState(0);
  const query = useSources({ limit: PAGE_SIZE, offset });
  const startCrawl = useStartCrawl();
  const update = useUpdateSource();
  const remove = useRemoveSource();
  const [confirmingId, setConfirmingId] = useState<string | null>(null);

  if (query.isPending) {
    return <p className="text-sm text-muted-foreground">Loading sources…</p>;
  }
  if (query.isError) {
    return <p role="alert" className="text-sm text-red-700">{describeError(query.error)}</p>;
  }

  const { items, total } = query.data;

  return (
    <div className="grid gap-3">
      <Table>
        <TableCaption className="sr-only">
          Registered sources: identity, product, live page and unit counts, last crawl, actions.
        </TableCaption>
        <TableHeader>
          <TableRow>
            <TableHead scope="col">Source</TableHead>
            <TableHead scope="col">Product</TableHead>
            <TableHead scope="col">Pages</TableHead>
            <TableHead scope="col">Units</TableHead>
            <TableHead scope="col">Last crawl</TableHead>
            <TableHead scope="col">Actions</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {items.length === 0 ? (
            <TableRow>
              <TableCell colSpan={6} className="text-muted-foreground">
                No sources registered yet. Nothing is searchable until one is.
              </TableCell>
            </TableRow>
          ) : (
            items.map((item) => (
              <TableRow key={item.id}>
                <TableCell>
                  <span className="block font-medium">{item.name}</span>
                  <Link
                    href={item.start_url}
                    target="_blank"
                    rel="noreferrer noopener"
                    className="text-xs text-muted-foreground underline underline-offset-4"
                  >
                    {item.start_url}
                  </Link>
                </TableCell>
                <TableCell>{item.product_domain ?? "—"}</TableCell>
                <TableCell className="font-mono">{item.page_count}</TableCell>
                <TableCell className="font-mono">{item.unit_count}</TableCell>
                <TableCell>
                  <LastCrawl at={item.last_crawl_at} status={item.last_crawl_status} />
                </TableCell>
                <TableCell>
                  <div className="flex flex-wrap items-center gap-2">
                    <button
                      type="button"
                      className="rounded-sm border px-2 py-1 text-xs"
                      onClick={() => startCrawl.mutate(item.id)}
                      disabled={startCrawl.isPending}
                    >
                      Crawl now
                    </button>
                    <button
                      type="button"
                      className="rounded-sm border px-2 py-1 text-xs"
                      aria-pressed={item.enabled}
                      onClick={() => update.mutate({ id: item.id, patch: { enabled: !item.enabled } })}
                      disabled={update.isPending}
                    >
                      {item.enabled ? "Disable" : "Enable"}
                    </button>
                    {confirmingId === item.id ? (
                      <span className="flex flex-wrap items-center gap-1 text-xs">
                        <span>Stop indexing this source?</span>
                        <button
                          type="button"
                          className="rounded-sm border border-red-700 px-2 py-1 text-xs text-red-700"
                          onClick={() => {
                            remove.mutate(item.id);
                            setConfirmingId(null);
                          }}
                        >
                          Confirm
                        </button>
                        <button
                          type="button"
                          className="rounded-sm border px-2 py-1 text-xs"
                          onClick={() => setConfirmingId(null)}
                        >
                          Keep
                        </button>
                      </span>
                    ) : (
                      <button
                        type="button"
                        className="rounded-sm border px-2 py-1 text-xs"
                        onClick={() => setConfirmingId(item.id)}
                      >
                        Remove
                      </button>
                    )}
                  </div>
                </TableCell>
              </TableRow>
            ))
          )}
        </TableBody>
      </Table>

      <p className="flex items-center gap-3 text-xs text-muted-foreground">
        <span>
          {items.length === 0 ? "no sources" : `${offset + 1}–${offset + items.length} of ${total}`}
        </span>
        <button
          type="button"
          className="rounded-sm border px-2 py-1 disabled:opacity-50"
          disabled={offset === 0}
          onClick={() => setOffset((value) => Math.max(0, value - PAGE_SIZE))}
        >
          Previous
        </button>
        <button
          type="button"
          className="rounded-sm border px-2 py-1 disabled:opacity-50"
          disabled={offset + PAGE_SIZE >= total}
          onClick={() => setOffset((value) => value + PAGE_SIZE)}
        >
          Next
        </button>
      </p>
    </div>
  );
}