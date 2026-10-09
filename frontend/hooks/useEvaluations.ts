"use client";

/**
 * React Query hooks for the evaluation surface (T149–T151).
 *
 * **Polling is tied to the run's own status, not to a wall-clock interval.** A
 * completed run should not keep causing requests, and a queued one should not be
 * assumed finished. `refetchInterval` answers while the run says it is in
 * flight and `false` once it is terminal — the same rule the registry hooks use
 * for a crawl in flight.
 *
 * **The empty state is a first-class response, not an error.** No run yet is
 * `items: []` with `total: 0`, and the dashboard renders an explanation. A
 * component that had to invent placeholder numbers to fill the gap would be
 * exactly the FR-037 failure.
 */
import {
  useMutation,
  useQuery,
  useQueryClient,
  type UseMutationResult,
  type UseQueryResult,
} from "@tanstack/react-query";
import {
  evaluations,
  type EvaluationResult,
  type EvaluationRun,
  type Page,
} from "@/lib/api-client";

/** Two seconds while a run is in flight — the run reports its own progress. */
const ACTIVE_POLL_MS = 2_000;
const IDLE_STALE_MS = 5_000;

export function isRunActive(status: EvaluationRun["status"] | undefined): boolean {
  return status === "queued" || status === "running";
}

export function useEvaluationRuns(): UseQueryResult<Page<EvaluationRun>> {
  return useQuery({
    queryKey: ["evaluation-runs"],
    queryFn: ({ signal }) => evaluations.listRuns({ limit: 20 }, signal),
    refetchInterval: (query) => {
      const items = query.state.data?.items;
      return items?.some((r) => isRunActive(r.status)) === true ? ACTIVE_POLL_MS : false;
    },
    staleTime: IDLE_STALE_MS,
  });
}

export function useEvaluationRun(runId: string | null): UseQueryResult<EvaluationRun> {
  return useQuery({
    queryKey: ["evaluation-run", runId],
    queryFn: ({ signal }) => evaluations.getRun(runId as string, signal),
    enabled: runId !== null,
    refetchInterval: (query) =>
      isRunActive(query.state.data?.status) ? ACTIVE_POLL_MS : false,
    staleTime: IDLE_STALE_MS,
  });
}

export function useEvaluationResults(runId: string | null): UseQueryResult<Page<EvaluationResult>> {
  return useQuery({
    queryKey: ["evaluation-results", runId],
    queryFn: ({ signal }) => evaluations.listResults(runId as string, { limit: 50 }, signal),
    enabled: runId !== null,
    staleTime: IDLE_STALE_MS,
  });
}

export function useStartEvaluationRun(): UseMutationResult<EvaluationRun, Error, void> {
  const client = useQueryClient();
  return useMutation({
    mutationFn: () => evaluations.startRun(),
    onSuccess: (run) => {
      void client.invalidateQueries({ queryKey: ["evaluation-runs"] });
      void client.invalidateQueries({ queryKey: ["evaluation-run", run.id] });
    },
  });
}