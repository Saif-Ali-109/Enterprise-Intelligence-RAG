"use client";

/**
 * React Query hooks for the registry: sources, documents, crawl jobs.
 *
 * **Polling, not websockets.** The API has no push channel, and inventing one
 * for a single-operator tool is a second protocol to keep alive. Instead, a view
 * that shows a live crawl asks for `refetchInterval` and the hook answers
 * 2 seconds while a job is in flight and `false` once it is not — a crawl that
 * finished 20 minutes ago should not still be causing requests.
 *
 * **`staleTime` is short but not zero** for the same reason. The corpus changes
 * only when a crawl runs, and a crawl is the thing the operator is watching, so
 * 5 seconds is enough to keep a list coherent without a request per render.
 */
import {
  useMutation,
  useQuery,
  useQueryClient,
  type UseMutationResult,
  type UseQueryResult,
} from "@tanstack/react-query";
import {
  crawlJobs,
  documents,
  sources,
  type CrawlJob,
  type CrawlJobListRequest,
  type CrawlJobStatus,
  type Document,
  type DocumentListRequest,
  type DocumentUnit,
  type Page,
  type RegisterSourceResult,
  type Source,
  type SourceCreate,
  type SourceListRequest,
  type SourceUpdate,
  type UnitListRequest,
} from "@/lib/api-client";

/** Half the time a crawl takes to fetch one page, at the configuration floor. */
const ACTIVE_POLL_MS = 2_000;
const IDLE_STALE_MS = 5_000;

/** Queued and running are the only states that can still change. */
export function isCrawlActive(status: CrawlJobStatus | undefined | null): boolean {
  return status === "queued" || status === "running";
}

/** Poll only while something in this page is unfinished. */
function pollWhileActive(jobs: readonly CrawlJob[] | undefined): number | false {
  if (jobs === undefined) {
    return false;
  }
  return jobs.some((job) => isCrawlActive(job.status)) ? ACTIVE_POLL_MS : false;
}

export function useSources(
  params: SourceListRequest = {},
): UseQueryResult<Page<Source>, Error> {
  return useQuery({
    queryKey: ["sources", params],
    queryFn: ({ signal }) => sources.list(params, signal),
    staleTime: IDLE_STALE_MS,
  });
}

export function useSource(id: string | null): UseQueryResult<Source, Error> {
  return useQuery({
    queryKey: ["sources", id],
    queryFn: ({ signal }) => sources.get(id ?? "", signal),
    enabled: id !== null,
    staleTime: IDLE_STALE_MS,
  });
}

export function useDocuments(
  params: DocumentListRequest = {},
): UseQueryResult<Page<Document>, Error> {
  return useQuery({
    queryKey: ["documents", params],
    queryFn: ({ signal }) => documents.list(params, signal),
    staleTime: IDLE_STALE_MS,
  });
}

export function useDocument(id: string | null): UseQueryResult<Document, Error> {
  return useQuery({
    queryKey: ["documents", id],
    queryFn: ({ signal }) => documents.get(id ?? "", signal),
    enabled: id !== null,
    staleTime: IDLE_STALE_MS,
  });
}

export function useDocumentUnits(
  id: string | null,
  params: UnitListRequest = {},
): UseQueryResult<Page<DocumentUnit>, Error> {
  return useQuery({
    queryKey: ["documents", id, "units", params],
    queryFn: ({ signal }) => documents.units(id ?? "", params, signal),
    enabled: id !== null,
    staleTime: IDLE_STALE_MS,
  });
}

export function useCrawlJobs(
  params: CrawlJobListRequest = {},
): UseQueryResult<Page<CrawlJob>, Error> {
  return useQuery({
    queryKey: ["crawl-jobs", params],
    queryFn: ({ signal }) => crawlJobs.list(params, signal),
    staleTime: IDLE_STALE_MS,
    refetchInterval: ({ state }) => pollWhileActive(state.data?.items),
  });
}

export function useCrawlJob(id: string | null): UseQueryResult<CrawlJob, Error> {
  return useQuery({
    queryKey: ["crawl-jobs", id],
    queryFn: ({ signal }) => crawlJobs.get(id ?? "", signal),
    enabled: id !== null,
    staleTime: IDLE_STALE_MS,
    refetchInterval: ({ state }) => (isCrawlActive(state.data?.status) ? ACTIVE_POLL_MS : false),
  });
}

/**
 * Register a source, then refresh everything that a new source changes.
 *
 * The crawl it starts is a *job*, and jobs are their own query — so the form
 * reports the job id and `crawl-status` watches it, rather than this mutation
 * waiting for a crawl that may take minutes. A registration that blocked until
 * the crawl finished would time out in every proxy between here and the API.
 */
export function useRegisterSource(): UseMutationResult<RegisterSourceResult, Error, SourceCreate> {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (payload: SourceCreate) => sources.register(payload),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ["sources"] });
      void client.invalidateQueries({ queryKey: ["crawl-jobs"] });
    },
  });
}

/** Request a re-crawl. A refusal arrives as a `CRAWL_ALREADY_RUNNING` `ApiError`. */
export function useStartCrawl(): UseMutationResult<CrawlJob, Error, string> {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (sourceId: string) => sources.crawl(sourceId),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ["crawl-jobs"] });
      void client.invalidateQueries({ queryKey: ["sources"] });
    },
  });
}

export function useUpdateSource(): UseMutationResult<Source, Error, { id: string; patch: SourceUpdate }> {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ id, patch }: { id: string; patch: SourceUpdate }) => sources.update(id, patch),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ["sources"] });
    },
  });
}

/**
 * Remove a source. Vectors go with it, so this is irreversible and the button
 * that triggers it says so; the API is idempotent, which means a retry is safe
 * even though the first attempt was not.
 */
export function useRemoveSource(): UseMutationResult<void, Error, string> {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (sourceId: string) => sources.remove(sourceId),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ["sources"] });
      void client.invalidateQueries({ queryKey: ["documents"] });
      void client.invalidateQueries({ queryKey: ["crawl-jobs"] });
    },
  });
}