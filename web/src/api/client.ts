/** Client API typé, généré depuis l'OpenAPI du backend (openapi-fetch). */

import createClient from "openapi-fetch";
import type { components, paths } from "./schema";

export const api = createClient<paths>({ baseUrl: "/" });

export type Schemas = components["schemas"];
export type Library = Schemas["LibraryOut"];
export type LibraryCreate = Schemas["LibraryCreate"];
export type LibraryPatch = Schemas["LibraryPatch"];
export type BitrateRule = Schemas["BitrateRuleOut"];
export type BitrateRuleIn = Schemas["BitrateRuleIn"];
export type Gpu = Schemas["GpuOut"];
export type GpuCreate = Schemas["GpuCreate"];
export type GpuPatch = Schemas["GpuPatch"];
export type MediaFile = Schemas["MediaFileOut"];
export type MediaFileDetail = Schemas["MediaFileDetail"];
export type NoGainReview = Schemas["NoGainReviewResult"];
export type MediaFilePage = Schemas["MediaFilePage"];
export type Job = Schemas["JobOut"];
export type JobDetail = Schemas["JobDetail"];
export type JobPage = Schemas["JobPage"];
export type ActiveJob = Schemas["ActiveJob"];
export type QueueStateT = Schemas["QueueState"];
export type Codec = Schemas["Codec"];
export type MediaStatus = Schemas["MediaStatus"];
export type JobState = Schemas["JobState"];
export type ResolutionBucket = Schemas["ResolutionBucket"];
export type HealthOut = Schemas["HealthOut"];
export type ConfigExport = Schemas["ConfigExport"];

/** Réponses des endpoints stats (typées côté back en dict → précisées ici). */
export interface StatsSummary {
  total_files: number;
  processed: number;
  by_status: Record<string, number>;
  bytes_saved: number;
  avg_gain_percent: number | null;
  throughput_files_per_hour: number;
  eta_s: number | null;
  queued: number;
  libraries_dry_run: { id: number; name: string; dry_run: boolean | null }[];
}

/** Granularité des courbes d'activité (créneaux UTC renvoyés par l'API). */
export type TimeUnit = "hour" | "day" | "week";

export interface SavingsStats {
  period_days: number | null;
  unit: TimeUnit;
  /** Espace récupéré sur la période (remplacements réels, hors dry-run) */
  total_bytes: number;
  files: number;
  avg_gain_percent: number | null;
  points: { t: string; bytes_saved: number; cumulative: number }[];
}

export interface CodecStats {
  current: Record<string, number>;
  replaced_sources: Record<string, number>;
}

export interface GainByBucket {
  resolution: string | null;
  codec: string | null;
  avg_gain_percent: number | null;
  count: number;
}

export interface ThroughputStats {
  period_days: number | null;
  unit: TimeUnit;
  files: number;
  gpu_hours: number;
  /** Fichiers réencodés par heure, en moyenne sur la période écoulée */
  files_per_hour: number | null;
  points: { t: string; files: number; gpu_hours: number }[];
  by_gpu: {
    gpu_id: number | null;
    files: number;
    avg_fps: number | null;
    gpu_hours: number;
  }[];
}

export interface TopRemainingItem {
  id: number;
  path: string;
  library_id: number;
  size_bytes: number | null;
  video_codec: string | null;
  resolution_bucket: string | null;
  estimated_gain_bytes: number | null;
  status: string;
}

export interface FailureCategoryStat {
  category: string;
  /** Tentatives en échec sur la période */
  jobs: number;
  /** Fichiers distincts concernés sur la période */
  files: number;
  /** Fichiers actuellement en échec définitif avec cette cause */
  failed_files: number;
  last_seen: string | null;
}

export interface FailureStats {
  period_days: number | null;
  total_jobs: number;
  failed_jobs: number;
  failure_rate: number;
  failed_files_total: number;
  categories: FailureCategoryStat[];
  top_errors: { error: string; category: string | null; count: number }[];
}

export interface ReliabilityStats {
  period_days: number | null;
  attempts: number;
  counts: {
    success: number;
    failed: number;
    no_gain: number;
    no_gain_early: number;
    no_gain_preflight: number;
    cancelled: number;
  };
  success_rate: number | null;
  attempt_success_rate: number | null;
  first_try_success: number;
  first_try_rate: number | null;
  recovered_by_fallback: number;
  files: {
    attempted: number;
    succeeded: number;
    no_gain: number;
    failed_now: number;
    retrying_now: number;
    predicted_no_gain: number;
  };
  gpu_hours: { success: number; failed: number; no_gain: number; cancelled: number };
  unit: TimeUnit;
  timeline: { t: string; success: number; failed: number; no_gain: number }[];
  strategies: { strategy: string; success: number; failed: number; no_gain: number }[];
}

/** Diagnostics d'un job (jobs.diagnostics, JSONB). */
export interface PreflightCheck {
  name: string;
  status: "ok" | "warning" | "error" | "skipped";
  detail: string;
}

export interface StreamDecisionInfo {
  index: number;
  kind: string;
  codec: string | null;
  action: "encode" | "copy" | "convert" | "drop";
  target_codec: string | null;
  language: string | null;
  title: string | null;
  reason: string | null;
}

export interface JobDiagnostics {
  preflight?: {
    checks: PreflightCheck[];
    gain: {
      predicted_gain_percent: number | null;
      src_video_kbps: number | null;
      target_kbps: number;
      bitrate_estimated: boolean;
      hopeless: boolean;
      reason: string;
    } | null;
    decode_errors: boolean;
  };
  stream_plan?: {
    streams: StreamDecisionInfo[];
    expected_audio: number;
    expected_subtitles: number;
    adjustments: number;
  };
  strategy?: {
    used: string;
    requested: string | null;
    gpu_api: string;
    attempt: number;
    container?: string;
    input_format?: string | null;
    /** Fichier final assemblé par mkvmerge (stratégie mkvmerge_mux) */
    mkvmerge?: boolean;
  };
  treatment?: { action: string; reason: string };
  decision?: {
    kind: "fallback" | "retry" | "final";
    next_strategy: string | null;
    retry_in_s: number | null;
    reason: string;
  };
}

export interface ValidationCheck {
  name: string;
  ok: boolean;
  detail: string;
  meta?: Record<string, unknown>;
}

export interface LibraryProgress {
  library_id: number;
  name: string;
  total: number;
  conform: number;
  percent: number;
}

export interface CapabilitiesInfo {
  ffmpeg_ok: boolean;
  ffmpeg_version: string | null;
  ffprobe_ok: boolean;
  libplacebo: boolean;
  libplacebo_dv: boolean;
  dovi_tool_ok: boolean;
  mkvpropedit_ok: boolean;
  gpus: {
    gpu_id: number;
    name: string;
    drm_node: string;
    configured_api: string;
    effective_api: string;
    fell_back_to_vaapi: boolean;
    encoders: Record<string, { ok: boolean; error: string | null }>;
    p5_zero_copy: boolean;
    alt_api: string | null;
    alt_encoders: Record<string, { ok: boolean; error: string | null }>;
  }[];
}

export async function fetchJson<T>(url: string): Promise<T> {
  const resp = await fetch(url);
  if (!resp.ok) throw new Error(`${url} → HTTP ${resp.status}`);
  return (await resp.json()) as T;
}

/** Période des statistiques : nombre de jours, ou null = depuis toujours. */
export type StatsPeriod = 1 | 7 | 30 | null;

export function periodQuery(days: StatsPeriod): string {
  return days === null ? "" : `?days=${days}`;
}
