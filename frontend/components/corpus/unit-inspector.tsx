"use client";

/**
 * The unit inspector (T158): the direct visual check that R-009's reconstruction
 * walk worked.
 *
 * **This view exists to make one property visible**: that the unit a citation
 * points at is the unit that text was cut from. Ordinals ascend from zero,
 * heading paths descend the page, and each unit's token size sits in the
 * 600–1000 band the chunker targets. A chunker that double-counted an overlap,
 * dropped a heading, or emitted a thousand-token runt would show it here, as a
 * number, before it reached an answer.
 *
 * **No unit text.** The registry stores provenance; the text lives in the
 * vector store and is read back at retrieval time. Rendering it here would be a
 * second copy of the corpus with none of the corpus's controls.
 */
import { useDocumentUnits } from "@/hooks/useRegistry";
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

export function UnitInspector({ documentId }: { documentId: string }) {
  const query = useDocumentUnits(documentId, { limit: 100 });

  if (query.isPending) {
    return <p className="text-sm text-muted-foreground">Loading units…</p>;
  }
  if (query.isError) {
    return <p role="alert" className="text-sm text-red-700">{describeError(query.error)}</p>;
  }

  const { items, total } = query.data;

  return (
    <Table>
      <TableCaption className="sr-only">
        The document&apos;s units in ordinal order with heading path and token size. Unit text is
        not stored in this application&apos;s database and is not shown here.
      </TableCaption>
      <TableHeader>
        <TableRow>
          <TableHead scope="col">#</TableHead>
          <TableHead scope="col">Heading path</TableHead>
          <TableHead scope="col">Blocks</TableHead>
          <TableHead scope="col">Tokens</TableHead>
          <TableHead scope="col">Overlap</TableHead>
          <TableHead scope="col">Vector id</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {items.length === 0 ? (
          <TableRow>
            <TableCell colSpan={6} className="text-muted-foreground">
              This document has no units, so it cannot support a citation.
            </TableCell>
          </TableRow>
        ) : (
          items.map((unit) => (
            <TableRow key={unit.id}>
              <TableCell className="font-mono">{unit.ordinal}</TableCell>
              <TableCell>
                {unit.heading_path.length === 0 ? (
                  <span className="text-muted-foreground">(page root)</span>
                ) : (
                  unit.heading_path.join(" › ")
                )}
              </TableCell>
              <TableCell className="text-xs">{unit.block_types.join(", ")}</TableCell>
              <TableCell className="font-mono">{unit.token_count}</TableCell>
              <TableCell className="font-mono text-muted-foreground">
                {unit.overlap_tokens}
              </TableCell>
              <TableCell className="font-mono text-xs text-muted-foreground">{unit.vector_id}</TableCell>
            </TableRow>
          ))
        )}
      </TableBody>
      {items.length < total ? (
        <TableBody>
          <TableRow>
            <TableCell colSpan={6} className="text-muted-foreground">
              {items.length} of {total} units shown.
            </TableCell>
          </TableRow>
        </TableBody>
      ) : null}
    </Table>
  );
}