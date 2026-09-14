"""Schémas Pydantic de l'API (OpenAPI généré → client TS typé, module web)."""

from __future__ import annotations

import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from transcoder.enums import (
    Codec,
    Container,
    DvPolicy,
    GpuApi,
    HdrFormat,
    HdrPolicy,
    JobState,
    MediaStatus,
    ResolutionBucket,
)


class _OrmModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------------------
# Bibliothèques
# ---------------------------------------------------------------------------
class BitrateRuleOut(_OrmModel):
    id: int
    codec: Codec
    resolution_bucket: ResolutionBucket
    is_hdr: bool
    target_bitrate_kbps: int
    max_bitrate_kbps: int
    bufsize_kbps: int


class BitrateRuleIn(BaseModel):
    codec: Codec
    resolution_bucket: ResolutionBucket
    is_hdr: bool
    target_bitrate_kbps: int = Field(gt=0)
    max_bitrate_kbps: int = Field(gt=0)
    bufsize_kbps: int = Field(gt=0)


class LibraryOut(_OrmModel):
    id: int
    name: str
    path: str
    enabled: bool
    paused: bool
    output_codec: Codec
    container: Container
    hdr_policy: HdrPolicy
    dv_policy: DvPolicy
    date_filter_after: datetime.datetime | None
    priority: int
    include_extensions: list[str]
    exclude_globs: list[str]
    min_gain_percent: float
    scan_cron: str | None
    dry_run: bool | None
    created_at: datetime.datetime
    updated_at: datetime.datetime


class LibraryCreate(BaseModel):
    name: str = Field(min_length=1)
    path: str = Field(min_length=1)
    output_codec: Codec = Codec.HEVC
    container: Container = Container.MKV
    hdr_policy: HdrPolicy = HdrPolicy.PRESERVE
    dv_policy: DvPolicy = DvPolicy.CONVERT_TO_HDR10
    date_filter_after: datetime.datetime | None = None
    priority: int = 0
    include_extensions: list[str] = Field(
        default_factory=lambda: ["mkv", "mp4", "m2ts", "ts", "avi", "mov", "wmv"]
    )
    exclude_globs: list[str] = Field(default_factory=list)
    min_gain_percent: float = 5.0
    scan_cron: str | None = "0 3 * * *"
    dry_run: bool | None = None
    enabled: bool = True


class LibraryPatch(BaseModel):
    name: str | None = None
    path: str | None = None
    enabled: bool | None = None
    paused: bool | None = None
    output_codec: Codec | None = None
    container: Container | None = None
    hdr_policy: HdrPolicy | None = None
    dv_policy: DvPolicy | None = None
    date_filter_after: datetime.datetime | None = None
    clear_date_filter: bool = False
    priority: int | None = None
    include_extensions: list[str] | None = None
    exclude_globs: list[str] | None = None
    min_gain_percent: float | None = None
    scan_cron: str | None = None
    clear_scan_cron: bool = False
    dry_run: bool | None = None
    clear_dry_run: bool = False


class CodecChangePreview(BaseModel):
    library_id: int
    current_codec: Codec
    target_codec: Codec
    requeued_count: int


class ScanTriggered(BaseModel):
    started: bool
    library_id: int


# ---------------------------------------------------------------------------
# GPU
# ---------------------------------------------------------------------------
class GpuOut(_OrmModel):
    id: int
    name: str
    drm_node: str
    api: GpuApi
    worker_count: int
    enabled: bool


class GpuCreate(BaseModel):
    name: str = Field(min_length=1)
    drm_node: str = Field(min_length=1)
    api: GpuApi = GpuApi.QSV
    worker_count: int = Field(default=1, ge=1, le=8)
    enabled: bool = True


class GpuPatch(BaseModel):
    name: str | None = None
    drm_node: str | None = None
    api: GpuApi | None = None
    worker_count: int | None = Field(default=None, ge=1, le=8)
    enabled: bool | None = None


class GpuTestResult(BaseModel):
    gpu_id: int
    results: dict[str, Any]


class DetectedNodes(BaseModel):
    nodes: list[str]


# ---------------------------------------------------------------------------
# Fichiers / queue / jobs
# ---------------------------------------------------------------------------
class MediaFileOut(_OrmModel):
    id: int
    library_id: int
    path: str
    size_bytes: int | None
    mtime: datetime.datetime | None
    status: MediaStatus
    video_codec: str | None
    width: int | None
    height: int | None
    resolution_bucket: ResolutionBucket | None
    video_bitrate_kbps: int | None
    is_hdr: bool | None
    hdr_format: HdrFormat | None
    dv_profile: int | None
    dv_bl_compat_id: int | None
    estimated_gain_bytes: int | None
    manual_rank: int | None
    retry_count: int
    last_error: str | None
    transcoder_tag: str | None
    applied_rule_hash: str | None
    failure_category: str | None = None
    attempt_count: int = 0
    strategies_tried: list[str] = Field(default_factory=list)
    next_strategy: str | None = None
    duration_s: float | None = None
    next_attempt_at: datetime.datetime | None = None
    # Nombre total de jobs (tentatives) enregistrés pour ce fichier
    job_count: int | None = None


class PageMeta(BaseModel):
    total: int
    limit: int
    offset: int


class MediaFilePage(BaseModel):
    items: list[MediaFileOut]
    meta: PageMeta


class JobOut(_OrmModel):
    id: int
    media_file_id: int
    library_id: int
    gpu_id: int | None
    state: JobState
    dry_run: bool
    started_at: datetime.datetime | None
    finished_at: datetime.datetime | None
    duration_s: float | None
    src_path: str | None
    src_size_bytes: int | None
    src_codec: str | None
    src_bitrate_kbps: int | None
    src_resolution: str | None
    src_hdr_format: HdrFormat | None
    dst_path: str | None
    dst_size_bytes: int | None
    dst_codec: str | None
    dst_bitrate_kbps: int | None
    dst_resolution: str | None
    dst_hdr_format: HdrFormat | None
    bytes_saved: int | None
    gain_percent: float | None
    avg_fps: float | None
    avg_speed: float | None
    exit_code: int | None
    error_message: str | None
    trashed_path: str | None
    attempt: int = 1
    strategy: str | None = None
    failure_category: str | None = None
    failure_detail: str | None = None


class JobDetail(JobOut):
    ffmpeg_cmd: list[str] | None
    log_tail: str | None
    log_path: str | None
    validation_report: dict[str, Any] | None
    diagnostics: dict[str, Any] | None = None


class MediaFileDetail(MediaFileOut):
    """Fichier + toutes ses tentatives (suivi des échecs et replis)."""

    jobs: list[JobOut] = Field(default_factory=list)
    audio_streams: list[dict[str, Any]] | None = None
    subtitle_streams: list[dict[str, Any]] | None = None


class RetryFailedRequest(BaseModel):
    """Relance des échecs définitifs : tous, par bibliothèque, par catégorie
    ou une sélection explicite de fichiers."""

    library_id: int | None = None
    failure_category: str | None = None
    file_ids: list[int] | None = None


class JobPage(BaseModel):
    items: list[JobOut]
    meta: PageMeta


class ActiveJob(BaseModel):
    job_id: int
    phase: str | None = None
    media_id: int
    library_id: int
    path: str
    gpu: str
    drm_node: str
    slot: int
    dry_run: bool
    started_at: str
    percent: float | None = None
    fps: float | None = None
    speed: float | None = None
    eta_s: float | None = None
    strategy: str | None = None
    attempt: int | None = None


class QueueState(BaseModel):
    paused: bool
    workers: int
    active: list[ActiveJob]
    waiting_count: int


class ReorderRequest(BaseModel):
    media_file_ids: list[int] = Field(min_length=1)


class ActionResult(BaseModel):
    ok: bool
    detail: str | None = None


# ---------------------------------------------------------------------------
# Settings / config / système
# ---------------------------------------------------------------------------
class SettingsOut(BaseModel):
    values: dict[str, Any]


class SettingsPatch(BaseModel):
    values: dict[str, Any] = Field(min_length=1)


class ConfigExport(BaseModel):
    version: int
    exported_at: datetime.datetime
    settings: dict[str, Any]
    gpus: list[dict[str, Any]]
    libraries: list[dict[str, Any]]


class ConfigImportResult(BaseModel):
    libraries: int
    gpus: int
    settings: int


class HealthOut(BaseModel):
    status: str
    database: bool
    workers: int
    dry_run_global: bool
    version: str = "dev"
    build_date: str | None = None
