"use client";

/**
 * The documents browser (T156): the indexed corpus, filterable, with every row
 * linking back to the page it came from.
 *
 * **Every row links to its original page.** The browser's job includes proving
 * that what is indexed here still says what it said when it was indexed, and a
 * row with no outbound link cannot be checked against anything.
 *
 * **Filters are server-side and honest about their vocabulary.** Product,
 * category, page type, and state are the query parameters the API declares; a
 * client-side select over a guess would report a different corpus than the one
 * being searched (T117's lesson, applied to browsing).
 *
 * **A deleted document stays browsable with its state shown.** A tombstone is
 * evidence — a page that disappeared upstream — so it is filtered by state
 * rather than hidden.
 */
import { useState } from "react";
import Link from "next/link";
import { useDocuments } from "@/hooks/useRegistry";
import { describeError, type DocumentState } from "@/lib/api-client";
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

/** The registry's own vocabulary, plus the empty value meaning "any". */
const STATES: (DocumentState | "")[] = [
  "",
  "discovered",
  "crawling",
  "processed",
  "indexed",
  "failed",
  "deleted",
];

export function DocumentTable() {
  const [product, setProduct] = useState("");
  const [category, setCategory] = useState("");
  const [state, setState] = useState<DocumentState | "">("");
  const [offset, setOffset] = useState(0);

  const query = useDocuments({
    limit: PAGE_SIZE,
    offset,
    ...(product === "" ? {} : { product }),
    ...(category === "" ? {} : { category }),
    ...(state === "" ? {} : { state }),
  });

  return (
    <div className="grid gap-3">
      <form
        className="flex flex-wrap items-end gap-3"
        onSubmit={(event) => {
          event.preventDefault();
          setOffset(0);
        }}
      >
        <label className="grid gap-1 text-xs">
          <span>Product</span>
          <input
            value={product}
            onChange={(event) => {
              setProduct(event.target.value.trim());
              setOffset(0);
            }}
            placeholder="jira"
            className="rounded-sm border px-2 py-1 text-sm"
          />
        </label>
        <label className="grid gap-1 text-xs">
          <span>Category</span>
          <input
            value={category}
            onChange={(event) => {
              setCategory(event.target.value.trim());
              setOffset(0);
            }}
            placeholder="filters"
            className="rounded-sm border px-2 py-1 text-sm"
          />
        </label>
        <label className="grid gap-1 text-xs">
          <span>State</span>
          <select
            value={state}
            onChange={(event) => {
              setState(event.target.value as DocumentState | "");
              setOffset(0);
            }}
            className="rounded-sm border px-2 py-1 text-sm"
          >
            {STATES.map((value) => (
              <option key={value} value={value}>
                {value === "" ? "any" : value}
              </option>
            ))}
          </select>
        </label>
      </form>

      {query.isPending ? <p className="text-sm text-muted-foreground">Loading documents…</p> : null}
      {query.isError ? (
        <p role="alert" className="text-sm text-red-700">
          {describeError(query.error)}
        </p>
      ) : null}

      {query.data !== undefined ? (
        <>
          <Table>
            <TableCaption className="sr-only">
              Indexed documents with product, category, state, unit count, and a link to the
              original page.
            </TableCaption>
            <TableHeader>
              <TableRow>
                <TableHead scope="col">Document</TableHead>
                <TableHead scope="col">Product</TableHead>
                <TableHead scope="col">Category</TableHead>
                <TableHead scope="col">State</TableHead>
                <TableHead scope="col">Units</TableHead>
                <TableHead scope="col">Indexed</TableHead>
                <TableHead scope="col">Detail</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {query.data.items.length === 0 ? (
                <TableRow>
                  <TableCell colSpan={7} className="text-muted-foreground">
                    {query.data.total === 0 && offset === 0
                      ? "No documents match. Nothing is searchable from these sources."
                      : "No documents on this page."}
                  </TableCell>
                </TableRow>
              ) : (
                query.data.items.map((item) => (
                  <TableRow key={item.id}>
                    <TableCell>
                      <span className="block font-medium">{item.title ?? item.url}</span>
                      <Link
                        href={item.url}
                        target="_blank"
                        rel="noreferrer noopener"
                        className="text-xs text-muted-foreground underline underline-offset-4"
                      >
                        {item.url}
                      </Link>
                    </TableCell>
                    <TableCell>{item.product ?? "—"}</TableCell>
                    <TableCell>{item.category ?? "—"}</TableCell>
                    <TableCell
                      className={item.state === "deleted" ? "text-red-700" : undefined}
                    >
                      {item.state}
                      {item.state_detail !== null ? (
                        <span className="block text-xs text-muted-foreground">
                          {item.state_detail}
                        </span>
                      ) : null}
                    </TableCell>
                    <TableCell className="font-mono">{item.unit_count}</TableCell>
                    <TableCell className="font-mono text-xs">
                      {item.indexed_at === null
                        ? "—"
                        : new Date(item.indexed_at).toISOString().slice(0, 10)}
                    </TableCell>
                    <TableCell>
                      <Link href={`/documents/${item.id}`} className="text-xs underline underline-offset-4">
                        Inspect
                      </Link>
                    </TableCell>
                  </TableRow>
                ))
              )}
            </TableBody>
          </Table>

          <p className="flex items-center gap-3 text-xs text-muted-foreground">
            <span>
              {query.data.total === 0
                ? "no documents"
                : `${offset + 1}–${offset + query.data.items.length} of ${query.data.total}`}
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
              disabled={offset + PAGE_SIZE >= query.data.total}
              onClick={() => setOffset((value) => value + PAGE_SIZE)}
            >
              Next
            </button>
          </p>
        </>
      ) : null}
    </div>
  );
}