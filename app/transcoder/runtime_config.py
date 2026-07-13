"""Configuration d'exploitation, lue depuis app_settings à chaque usage.

Toute la config runtime vit en base et est rechargée à chaud : le scanner
et chaque worker rechargent une RuntimeConfig avant chaque cycle — aucune
modification ne nécessite de redémarrage (§3).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from transcoder.models import AppSetting


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    """Vue typée d'app_settings au moment du chargement."""

    ffmpeg_path: str
    ffprobe_path: str
    dovi_tool_path: str
    mkvpropedit_path: str
    tmp_strategy: str
    tmp_dir: str
    trash_dir: str
    dry_run_out_dir: str
    log_dir: str
    trash_retention_days: int
    log_retention_days: int
    immediate_delete: bool
    dry_run_global: bool
    max_concurrent_jobs: int
    min_free_disk_gb: float
    queue_paused: bool
    dv_ssim_threshold: float
    dv_chroma_delta_max: float
    validation_decode: str
    max_retries: int
    early_abort_no_gain: bool
    progress_snapshot_interval_s: int
    jellyfin_url: str | None
    jellyfin_api_key: str | None


def _str(values: dict[str, Any], key: str, default: str) -> str:
    v = values.get(key)
    return v if isinstance(v, str) and v else default


def _opt_str(values: dict[str, Any], key: str) -> str | None:
    v = values.get(key)
    return v if isinstance(v, str) and v else None


def _int(values: dict[str, Any], key: str, default: int) -> int:
    v = values.get(key)
    return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else default


def _float(values: dict[str, Any], key: str, default: float) -> float:
    v = values.get(key)
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else default


def _bool(values: dict[str, Any], key: str, default: bool) -> bool:
    v = values.get(key)
    return v if isinstance(v, bool) else default


def config_from_values(values: dict[str, Any]) -> RuntimeConfig:
    """Construit la vue typée (fonction pure, testée sans DB)."""
    return RuntimeConfig(
        ffmpeg_path=_str(values, "ffmpeg_path", "/usr/lib/jellyfin-ffmpeg/ffmpeg"),
        ffprobe_path=_str(values, "ffprobe_path", "/usr/lib/jellyfin-ffmpeg/ffprobe"),
        dovi_tool_path=_str(values, "dovi_tool_path", "/usr/local/bin/dovi_tool"),
        mkvpropedit_path=_str(values, "mkvpropedit_path", "/usr/bin/mkvpropedit"),
        tmp_strategy=_str(values, "tmp_strategy", "sibling"),
        tmp_dir=_str(values, "tmp_dir", "/data/tmp"),
        trash_dir=_str(values, "trash_dir", "/data/trash"),
        dry_run_out_dir=_str(values, "dry_run_out_dir", "/data/out"),
        log_dir=_str(values, "log_dir", "/data/logs"),
        trash_retention_days=_int(values, "trash_retention_days", 14),
        log_retention_days=_int(values, "log_retention_days", 90),
        immediate_delete=_bool(values, "immediate_delete", False),
        dry_run_global=_bool(values, "dry_run_global", True),
        max_concurrent_jobs=_int(values, "max_concurrent_jobs", 1),
        min_free_disk_gb=_float(values, "min_free_disk_gb", 20.0),
        queue_paused=_bool(values, "queue_paused", False),
        dv_ssim_threshold=_float(values, "dv_ssim_threshold", 0.90),
        dv_chroma_delta_max=_float(values, "dv_chroma_delta_max", 8.0),
        validation_decode=_str(values, "validation_decode", "full"),
        max_retries=_int(values, "max_retries", 2),
        early_abort_no_gain=_bool(values, "early_abort_no_gain", True),
        progress_snapshot_interval_s=_int(values, "progress_snapshot_interval_s", 10),
        jellyfin_url=_opt_str(values, "jellyfin_url"),
        jellyfin_api_key=_opt_str(values, "jellyfin_api_key"),
    )


async def load_runtime_config(session: AsyncSession) -> RuntimeConfig:
    rows = (await session.execute(select(AppSetting))).scalars().all()
    return config_from_values({r.key: r.value for r in rows})
