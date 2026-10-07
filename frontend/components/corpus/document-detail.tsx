"use client";

/**
 * The document detail view (T157): everything the registry knows about one
 * page, and nothing it does not store.
 *
 * **Fingerprint and lifecycle state are on this screen because they are how a
 * citation is traced.** A reader who wants to know why an answer cited a
 * certain section needs to reach the unit, and the unit is only findable from
 * this document.
 *
 * **`state` with its `state_detail` reads as a sentence, not a code.** The
 * stored reason (`upstream 404`, `content below the minimum useful size`) is
 * the answer to "why isn't this page searchable", and a bare enum cannot answer
 * it.
 */
import Link from "next/link";
import { useDocument } from "@/hooks/useRegistry";
import { describeError } from "@/lib/api-client";
import { UnitInspector } from "@/components/corpus/unit-inspector";

function Row({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="grid grid-cols-[10rem_1fr] gap-2 border-b py-1 last:border-b-0">
      <dt className="text-xs text-muted-foreground">{label}</dt>
      <dd className="text-sm">{value}</dd>
    </div>
  );
}

function stamp(value: string | null): string {
  return value === null ? "—" : new Date(value).toISOString().replace("T", " ").slice(0, 19);
}

export function DocumentDetail({ documentId }: { documentId: string }) {
  const query = useDocument(documentId);

  if (query.isPending) {
    return <p className="text-sm text-muted-foreground">Loading document…</p>;
  }
  if (query.isError) {
    return (
      <p role="alert" className="text-sm text-red-700">
        This document could not be loaded. {describeError(query.error)}
      </p>
    );
  }

  const item = query.data;

  return (
    <div className="grid gap-6">
      <div className="grid gap-1">
        <h2 className="text-lg font-semibold">{item.title ?? item.url}</h2>
        <Link
          href={item.url}
          target="_blank"
          rel="noreferrer noopener"
          className="text-xs text-muted-foreground underline underline-offset-4"
        >
          {item.url}
        </Link>
      </div>

      <dl>
        <Row label="Product" value={item.product ?? "—"} />
        <Row label="Category" value={item.category ?? "—"} />
        <Row label="Page type" value={item.page_type ?? "—"} />
        <Row label="Language" value={item.language ?? "—"} />
        <Row label="State" value={item.state_detail ?? item.state} />
        <Row
          label="Vectors"
          value={item.vectors_live ? "live in the index" : "not live (tombstoned or superseded)"}
        />
        <Row label="Units" value={<span className="font-mono">{item.unit_count}</span>} />
        <Row
          label="Fingerprint"
          value={<span className="font-mono text-xs">{item.content_fingerprint ?? "—"}</span>}
        />
        <Row label="Last crawled" value={<span className="font-mono text-xs">{stamp(item.last_crawled_at)}</span>} />
        <Row label="Indexed" value={<span className="font-mono text-xs">{stamp(item.indexed_at)}</span>} />
        {item.tombstoned_at !== null ? (
          <Row label="Tombstoned" value={<span className="font-mono text-xs">{stamp(item.tombstoned_at)}</span>} />
        ) : null}
      </dl>

      <section aria-labelledby="units-heading" className="grid gap-2">
        <h3 id="units-heading" className="text-sm font-semibold">
          Units
        </h3>
        <p className="text-xs text-muted-foreground">
          The chunks this page was cut into, in reconstruction order. This is what a citation
          resolves to.
        </p>
        <UnitInspector documentId={documentId} />
      </section>
    </div>
  );
}