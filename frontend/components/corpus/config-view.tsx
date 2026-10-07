"use client";

/**
 * The configuration view (T160): what this instance is actually running, and
 * what it has.
 *
 * **Secrets appear as presence, never as values.** `secrets.pinecone_api_key`
 * is `{"configured": true}` — the schema has no field a value could occupy, so
 * this view cannot render one even by accident (FR-042).
 *
 * **`corpus` is counted, not configured.** The source/document/unit counts come
 * from the API's live rows with tombstones excluded; the rest is the effective
 * configuration after defaults and environment. A view that showed only the
 * configured values would tell an operator what they asked for, not what the
 * process is doing.
 *
 * **`reasoning_exposed` is shown because it is a claim worth checking.** It is
 * `false` by construction (FR-034) and rendering it lets an operator verify
 * that against a running system rather than trusting the documentation.
 */
import { useQuery } from "@tanstack/react-query";
import { describeError, system } from "@/lib/api-client";

function Line({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="grid grid-cols-[12rem_1fr] gap-2 border-b py-1 last:border-b-0">
      <dt className="text-xs text-muted-foreground">{label}</dt>
      <dd className="text-sm">{value}</dd>
    </div>
  );
}

export function ConfigView() {
  const query = useQuery({ queryKey: ["config"], queryFn: ({ signal }) => system.config(signal) });

  if (query.isPending) {
    return <p className="text-sm text-muted-foreground">Loading configuration…</p>;
  }
  if (query.isError) {
    return (
      <p role="alert" className="text-sm text-red-700">
        {describeError(query.error)}
      </p>
    );
  }

  const config = query.data;

  return (
    <div className="grid gap-6">
      <section aria-labelledby="config-corpus" className="grid gap-2">
        <h3 id="config-corpus" className="text-sm font-semibold">
          Corpus (counted from live rows)
        </h3>
        <dl>
          <Line label="Sources" value={<span className="font-mono">{config.corpus.source_count}</span>} />
          <Line
            label="Documents"
            value={<span className="font-mono">{config.corpus.document_count}</span>}
          />
          <Line label="Units" value={<span className="font-mono">{config.corpus.unit_count}</span>} />
          <Line label="Allowed domains" value={config.corpus.allowed_domains.join(", ") || "—"} />
        </dl>
      </section>

      <section aria-labelledby="config-retrieval" className="grid gap-2">
        <h3 id="config-retrieval" className="text-sm font-semibold">
          Retrieval
        </h3>
        <dl>
          <Line label="Candidate pool" value={config.retrieval.candidate_pool} />
          <Line label="Rerank top n" value={config.retrieval.rerank_top_n} />
          <Line
            label="Evidence units"
            value={`${config.retrieval.evidence_min}–${config.retrieval.evidence_max}`}
          />
          <Line label="Filters" value={config.retrieval.filters_enabled ? "enabled" : "disabled"} />
          {config.retrieval.min_rerank_score !== undefined ? (
            <Line label="Min rerank score" value={config.retrieval.min_rerank_score} />
          ) : (
            <Line
              label="Min rerank score"
              value={<span className="text-muted-foreground">unset (no floor in force)</span>}
            />
          )}
          {config.retrieval.min_evidence_score !== undefined ? (
            <Line label="Min evidence score" value={config.retrieval.min_evidence_score} />
          ) : (
            <Line
              label="Min evidence score"
              value={<span className="text-muted-foreground">unset (no floor in force)</span>}
            />
          )}
        </dl>
      </section>

      <section aria-labelledby="config-generation" className="grid gap-2">
        <h3 id="config-generation" className="text-sm font-semibold">
          Generation and index
        </h3>
        <dl>
          <Line label="Provider" value={config.generation.provider} />
          <Line label="Model" value={<span className="font-mono">{config.generation.model}</span>} />
          <Line
            label="Classification model"
            value={<span className="font-mono">{config.generation.classification_model}</span>}
          />
          <Line label="Max attempts" value={config.generation.max_attempts} />
          <Line
            label="Reasoning exposed"
            value={
              config.generation.reasoning_exposed ? (
                <span className="text-red-700">true — this is a contract violation</span>
              ) : (
                "false"
              )
            }
          />
          <Line label="Index" value={<span className="font-mono">{config.index.name}</span>} />
          <Line label="Namespace" value={<span className="font-mono">{config.index.namespace}</span>} />
          <Line label="Embed model" value={<span className="font-mono">{config.index.embed_model}</span>} />
          <Line label="Dimension source" value={config.index.dimension_source} />
          <Line label="Rerank model" value={<span className="font-mono">{config.index.rerank_model}</span>} />
        </dl>
      </section>

      <section aria-labelledby="config-secrets" className="grid gap-2">
        <h3 id="config-secrets" className="text-sm font-semibold">
          Secrets (presence only)
        </h3>
        <dl>
          {Object.entries(config.secrets).map(([name, presence]) => (
            <Line
              key={name}
              label={name}
              value={presence.configured ? "configured" : "not configured"}
            />
          ))}
        </dl>
      </section>
    </div>
  );
}