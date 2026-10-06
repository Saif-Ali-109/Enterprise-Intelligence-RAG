import type { Citation } from "@/lib/api-client";

/**
 * Which sources contributed, and from which products (T117).
 *
 * **Derived from the validated citations, not added to the contract.** The
 * contract closes `AskResponse` with `additionalProperties: false` and has no
 * source-count field; adding one would put a number in the response that a client
 * could read but the contract could not describe. Counting distinct
 * `source_url`s and `product`s in the citations that already arrived means the
 * count cannot disagree with the evidence list — it *is* the evidence list,
 * counted.
 *
 * **A count of one is stated, not implied.** "1 source" and "3 sources across 2
 * products" are both sentences an operator can act on; a bare number of cards
 * is neither.
 */
export interface SourceSummary {
  /** Distinct publisher pages among the validated citations. */
  sourceCount: number;
  /** Distinct products (`jira`, `confluence`, …) among them. */
  productCount: number;
  /** Sorted, de-duplicated. Empty when the response cited nothing. */
  products: string[];
  /** Sorted, de-duplicated source URLs. */
  urls: string[];
}

export function summariseSources(citations: Citation[]): SourceSummary {
  const urls = [...new Set(citations.map((citation) => citation.source_url))].sort();
  const products = [
    ...new Set(
      citations
        .map((citation) => citation.product)
        .filter((product): product is string => product !== null && product !== ""),
    ),
  ].sort();
  return {
    sourceCount: urls.length,
    productCount: products.length,
    products,
    urls,
  };
}

/** The sentence shown above the citation list. */
export function describeSources(summary: SourceSummary): string {
  if (summary.sourceCount === 0) {
    return "No sources contributed to this answer.";
  }
  if (summary.productCount <= 1) {
    return `${summary.sourceCount} ${summary.sourceCount === 1 ? "source" : "sources"}.`;
  }
  return (
    `${summary.sourceCount} sources across ${summary.productCount} products: ` +
    `${summary.products.join(", ")}.`
  );
}
