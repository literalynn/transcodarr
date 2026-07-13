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

export interface SavingsPoint {
  day: string;
  bytes_saved: number;
  cumulative: number;
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
  per_day: { day: string; files: number; gpu_hours: number }[];
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

export interface FailureStats {
  total_jobs: number;
  failed_jobs: number;
  failure_rate: number;
  top_errors: { error: string; count: number }[];
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
  }[];
}

export async function fetchJson<T>(url: string): Promise<T> {
  const resp = await fetch(url);
  if (!resp.ok) throw new Error(`${url} → HTTP ${resp.status}`);
  return (await resp.json()) as T;
}
