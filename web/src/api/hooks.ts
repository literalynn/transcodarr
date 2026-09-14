/** Hooks TanStack Query sur le client API typé. */

import {
  useMutation,
  useQuery,
  useQueryClient,
  type UseQueryResult,
} from "@tanstack/react-query";
import {
  api,
  fetchJson,
  type BitrateRuleIn,
  type CapabilitiesInfo,
  type CodecStats,
  type FailureStats,
  type GainByBucket,
  type GpuCreate,
  type GpuPatch,
  type LibraryCreate,
  type LibraryPatch,
  type LibraryProgress,
  type ReliabilityStats,
  type SavingsStats,
  type StatsPeriod,
  periodQuery,
  type StatsSummary,
  type ThroughputStats,
  type TopRemainingItem,
} from "./client";

export function useLibraries() {
  return useQuery({
    queryKey: ["libraries"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/v1/libraries");
      if (error) throw error;
      return data;
    },
  });
}

export function useBitrateRules(libraryId: number | null) {
  return useQuery({
    queryKey: ["bitrate-rules", libraryId],
    enabled: libraryId !== null,
    queryFn: async () => {
      const { data, error } = await api.GET("/api/v1/libraries/{library_id}/bitrate-rules", {
        params: { path: { library_id: libraryId ?? 0 } },
      });
      if (error) throw error;
      return data;
    },
  });
}

export function useGpus() {
  return useQuery({
    queryKey: ["gpus"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/v1/gpus");
      if (error) throw error;
      return data;
    },
  });
}

export function useQueueState() {
  return useQuery({
    queryKey: ["queue"],
    refetchInterval: 5000,
    queryFn: async () => {
      const { data, error } = await api.GET("/api/v1/queue");
      if (error) throw error;
      return data;
    },
  });
}

export interface FilesParams {
  library_id?: number;
  status?: string;
  failure_category?: string;
  search?: string;
  order?: string;
  with_job_count?: boolean;
  limit?: number;
  offset?: number;
}

export function useFiles(params: FilesParams, options?: { refetchInterval?: number }) {
  return useQuery({
    queryKey: ["files", params],
    refetchInterval: options?.refetchInterval,
    placeholderData: (previous) => previous,
    queryFn: async () => {
      const { data, error } = await api.GET("/api/v1/files", {
        params: {
          query: {
            library_id: params.library_id,
            status: params.status as never,
            failure_category: params.failure_category,
            search: params.search || undefined,
            order: params.order,
            with_job_count: params.with_job_count,
            limit: params.limit,
            offset: params.offset,
          },
        },
      });
      if (error) throw error;
      return data;
    },
  });
}

export function useFileDetail(fileId: number | null) {
  return useQuery({
    queryKey: ["file", fileId],
    enabled: fileId !== null,
    queryFn: async () => {
      const { data, error } = await api.GET("/api/v1/files/{file_id}", {
        params: { path: { file_id: fileId ?? 0 } },
      });
      if (error) throw error;
      return data;
    },
  });
}

export interface JobsParams {
  library_id?: number;
  state?: string;
  outcome?: string;
  failure_category?: string;
  strategy?: string;
  media_file_id?: number;
  search?: string;
  sort?: string;
  limit?: number;
  offset?: number;
}

export function useJobs(params: JobsParams) {
  return useQuery({
    queryKey: ["jobs", params],
    placeholderData: (previous) => previous,
    queryFn: async () => {
      const { data, error } = await api.GET("/api/v1/jobs", {
        params: {
          query: {
            library_id: params.library_id,
            state: params.state as never,
            outcome: params.outcome,
            failure_category: params.failure_category,
            strategy: params.strategy,
            media_file_id: params.media_file_id,
            search: params.search || undefined,
            sort: params.sort,
            limit: params.limit,
            offset: params.offset,
          },
        },
      });
      if (error) throw error;
      return data;
    },
  });
}

export function useJobDetail(jobId: number | null) {
  return useQuery({
    queryKey: ["job", jobId],
    enabled: jobId !== null,
    queryFn: async () => {
      const { data, error } = await api.GET("/api/v1/jobs/{job_id}", {
        params: { path: { job_id: jobId ?? 0 } },
      });
      if (error) throw error;
      return data;
    },
  });
}

export function useHealth() {
  return useQuery({
    queryKey: ["health"],
    refetchInterval: 15000,
    queryFn: async () => {
      const { data, error } = await api.GET("/api/v1/health");
      if (error) throw error;
      return data;
    },
  });
}

export function useSettings() {
  return useQuery({
    queryKey: ["settings"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/v1/settings");
      if (error) throw error;
      return data;
    },
  });
}

export function useCapabilities(): UseQueryResult<CapabilitiesInfo> {
  return useQuery({
    queryKey: ["capabilities"],
    queryFn: () => fetchJson<CapabilitiesInfo>("/api/v1/capabilities"),
  });
}

// --- Stats (§7.4) -----------------------------------------------------------
export const useStatsSummary = (): UseQueryResult<StatsSummary> =>
  useQuery({
    queryKey: ["stats", "summary"],
    refetchInterval: 15000,
    queryFn: () => fetchJson<StatsSummary>("/api/v1/stats/summary"),
  });

export const useSavingsOverTime = (days: StatsPeriod): UseQueryResult<SavingsStats> =>
  useQuery({
    queryKey: ["stats", "savings", days],
    refetchInterval: 60000,
    placeholderData: (previous) => previous,
    queryFn: () => fetchJson<SavingsStats>(`/api/v1/stats/savings-over-time${periodQuery(days)}`),
  });

export const useCodecStats = (): UseQueryResult<CodecStats> =>
  useQuery({
    queryKey: ["stats", "codecs"],
    queryFn: () => fetchJson<CodecStats>("/api/v1/stats/codecs"),
  });

export const useGainByBucket = (days: StatsPeriod): UseQueryResult<GainByBucket[]> =>
  useQuery({
    queryKey: ["stats", "gain-by-bucket", days],
    refetchInterval: 60000,
    placeholderData: (previous) => previous,
    queryFn: () => fetchJson<GainByBucket[]>(`/api/v1/stats/gain-by-bucket${periodQuery(days)}`),
  });

export const useThroughput = (days: StatsPeriod): UseQueryResult<ThroughputStats> =>
  useQuery({
    queryKey: ["stats", "throughput", days],
    refetchInterval: 60000,
    placeholderData: (previous) => previous,
    queryFn: () => fetchJson<ThroughputStats>(`/api/v1/stats/throughput${periodQuery(days)}`),
  });

export const useTopRemaining = (): UseQueryResult<TopRemainingItem[]> =>
  useQuery({
    queryKey: ["stats", "top-remaining"],
    queryFn: () => fetchJson<TopRemainingItem[]>("/api/v1/stats/top-remaining"),
  });

export const useFailures = (days: StatsPeriod = null): UseQueryResult<FailureStats> =>
  useQuery({
    queryKey: ["stats", "failures", days],
    refetchInterval: 30000,
    placeholderData: (previous) => previous,
    queryFn: () => fetchJson<FailureStats>(`/api/v1/stats/failures${periodQuery(days)}`),
  });

export const useReliability = (days: StatsPeriod): UseQueryResult<ReliabilityStats> =>
  useQuery({
    queryKey: ["stats", "reliability", days],
    refetchInterval: 30000,
    placeholderData: (previous) => previous,
    queryFn: () => fetchJson<ReliabilityStats>(`/api/v1/stats/reliability${periodQuery(days)}`),
  });

export const useLibraryProgress = (): UseQueryResult<LibraryProgress[]> =>
  useQuery({
    queryKey: ["stats", "progress-by-library"],
    queryFn: () => fetchJson<LibraryProgress[]>("/api/v1/stats/progress-by-library"),
  });

// --- Mutations ---------------------------------------------------------------
export function useInvalidate() {
  const qc = useQueryClient();
  return (...keys: string[]) => {
    for (const key of keys) void qc.invalidateQueries({ queryKey: [key] });
  };
}

export function useCreateLibrary() {
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: async (body: LibraryCreate) => {
      const { data, error } = await api.POST("/api/v1/libraries", { body });
      if (error) throw error;
      return data;
    },
    onSuccess: () => invalidate("libraries"),
  });
}

export function usePatchLibrary() {
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: async (args: { id: number; patch: LibraryPatch }) => {
      const { data, error } = await api.PATCH("/api/v1/libraries/{library_id}", {
        params: { path: { library_id: args.id } },
        body: args.patch,
      });
      if (error) throw error;
      return data;
    },
    onSuccess: () => invalidate("libraries", "files", "queue"),
  });
}

export function useDeleteLibrary() {
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: async (id: number) => {
      const { error } = await api.DELETE("/api/v1/libraries/{library_id}", {
        params: { path: { library_id: id } },
      });
      if (error) throw error;
    },
    onSuccess: () => invalidate("libraries", "files", "queue", "stats"),
  });
}

export function usePutBitrateRules() {
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: async (args: { libraryId: number; rules: BitrateRuleIn[] }) => {
      const { data, error } = await api.PUT("/api/v1/libraries/{library_id}/bitrate-rules", {
        params: { path: { library_id: args.libraryId } },
        body: args.rules,
      });
      if (error) throw error;
      return data;
    },
    onSuccess: () => invalidate("bitrate-rules"),
  });
}

export function useTriggerScan() {
  return useMutation({
    mutationFn: async (libraryId: number) => {
      const { data, error } = await api.POST("/api/v1/libraries/{library_id}/scan", {
        params: { path: { library_id: libraryId } },
      });
      if (error) throw error;
      return data;
    },
  });
}

export function useQueueAction() {
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: async (args: {
      action: "pause" | "resume";
      libraryId?: number;
    }) => {
      const path =
        args.action === "pause" ? "/api/v1/queue/pause" : "/api/v1/queue/resume";
      const { data, error } = await api.POST(path, {
        params: { query: { library_id: args.libraryId } },
      });
      if (error) throw error;
      return data;
    },
    onSuccess: () => invalidate("queue", "health", "libraries", "settings"),
  });
}

export function useReorderQueue() {
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: async (mediaFileIds: number[]) => {
      const { data, error } = await api.POST("/api/v1/queue/reorder", {
        body: { media_file_ids: mediaFileIds },
      });
      if (error) throw error;
      return data;
    },
    onSuccess: () => invalidate("files", "queue"),
  });
}

export function useFileAction() {
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: async (args: { id: number; action: "force" | "ignore" | "reset" }) => {
      const path = `/api/v1/files/{file_id}/${args.action}` as const;
      const { data, error } = await api.POST(path as "/api/v1/files/{file_id}/force", {
        params: { path: { file_id: args.id } },
      });
      if (error) throw error;
      return data;
    },
    onSuccess: () => invalidate("files", "queue", "stats", "file"),
  });
}

export function useJobAction() {
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: async (args: { id: number; action: "cancel" | "retry" }) => {
      const path = `/api/v1/jobs/{job_id}/${args.action}` as const;
      const { data, error } = await api.POST(path as "/api/v1/jobs/{job_id}/cancel", {
        params: { path: { job_id: args.id } },
      });
      if (error) throw error;
      return data;
    },
    onSuccess: () => invalidate("jobs", "queue", "files", "file", "stats"),
  });
}

export function useRetryFailed() {
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: async (body: {
      library_id?: number;
      failure_category?: string;
      file_ids?: number[];
    }) => {
      const { data, error } = await api.POST("/api/v1/files/retry-failed", { body });
      if (error) throw error;
      return data;
    },
    onSuccess: () => invalidate("files", "queue", "stats", "jobs", "file"),
  });
}

export function usePatchSettings() {
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: async (values: Record<string, unknown>) => {
      const { data, error } = await api.PATCH("/api/v1/settings", {
        body: { values },
      });
      if (error) throw error;
      return data;
    },
    onSuccess: () => invalidate("settings", "health", "queue"),
  });
}

export function useGpuMutations() {
  const invalidate = useInvalidate();
  const create = useMutation({
    mutationFn: async (body: GpuCreate) => {
      const { data, error } = await api.POST("/api/v1/gpus", { body });
      if (error) throw error;
      return data;
    },
    onSuccess: () => invalidate("gpus", "capabilities"),
  });
  const patch = useMutation({
    mutationFn: async (args: { id: number; patch: GpuPatch }) => {
      const { data, error } = await api.PATCH("/api/v1/gpus/{gpu_id}", {
        params: { path: { gpu_id: args.id } },
        body: args.patch,
      });
      if (error) throw error;
      return data;
    },
    onSuccess: () => invalidate("gpus", "capabilities"),
  });
  const remove = useMutation({
    mutationFn: async (id: number) => {
      const { error } = await api.DELETE("/api/v1/gpus/{gpu_id}", {
        params: { path: { gpu_id: id } },
      });
      if (error) throw error;
    },
    onSuccess: () => invalidate("gpus", "capabilities"),
  });
  const test = useMutation({
    mutationFn: async (id: number) => {
      const { data, error } = await api.POST("/api/v1/gpus/{gpu_id}/test", {
        params: { path: { gpu_id: id } },
      });
      if (error) throw error;
      return data;
    },
    onSuccess: () => invalidate("capabilities"),
  });
  return { create, patch, remove, test };
}
