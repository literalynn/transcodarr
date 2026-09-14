"""Réglages, export/import de config, santé, capacités (§6, §9.5.10)."""

from __future__ import annotations

import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from transcoder.api import schemas
from transcoder.api.deps import get_capabilities, get_queue_manager, get_scan_scheduler
from transcoder.core.capabilities import Capabilities
from transcoder.core.queue import QueueManager
from transcoder.db import get_db
from transcoder.models import AppSetting, BitrateRule, Gpu, Library
from transcoder.runtime_config import load_runtime_config
from transcoder.scan_service import ScanScheduler

router = APIRouter(tags=["system"])

# Clés autorisées dans app_settings : tout le reste est refusé (typos, injection)
_ALLOWED_SETTINGS: frozenset[str] = frozenset(
    {
        "ffmpeg_path", "ffprobe_path", "dovi_tool_path", "mkvpropedit_path",
        "tmp_strategy", "tmp_dir", "trash_dir", "dry_run_out_dir", "log_dir",
        "trash_retention_days", "log_retention_days", "immediate_delete",
        "dry_run_global", "max_concurrent_jobs", "min_free_disk_gb",
        "queue_paused", "dv_ssim_threshold", "dv_chroma_delta_max",
        "validation_decode", "max_retries", "progress_snapshot_interval_s",
        "early_abort_no_gain", "preflight_gain_check", "preflight_decode_check",
        "auto_fallback",
        "jellyfin_url", "jellyfin_api_key",
    }
)


@router.get("/settings", response_model=schemas.SettingsOut)
async def get_settings_endpoint(session: AsyncSession = Depends(get_db)) -> schemas.SettingsOut:
    rows = (await session.execute(select(AppSetting))).scalars().all()
    return schemas.SettingsOut(values={r.key: r.value for r in rows})


@router.patch("/settings", response_model=schemas.SettingsOut)
async def patch_settings(
    payload: schemas.SettingsPatch, session: AsyncSession = Depends(get_db)
) -> schemas.SettingsOut:
    unknown = set(payload.values) - _ALLOWED_SETTINGS
    if unknown:
        raise HTTPException(422, f"clés inconnues : {sorted(unknown)}")
    if "validation_decode" in payload.values and payload.values["validation_decode"] not in (
        "full",
        "sample",
    ):
        raise HTTPException(422, "validation_decode doit être 'full' ou 'sample'")
    if "tmp_strategy" in payload.values and payload.values["tmp_strategy"] not in (
        "sibling",
        "data_tmp",
    ):
        raise HTTPException(422, "tmp_strategy doit être 'sibling' ou 'data_tmp'")
    for key, value in payload.values.items():
        stmt = (
            pg_insert(AppSetting)
            .values(key=key, value=value)
            .on_conflict_do_update(index_elements=[AppSetting.key], set_={"value": value})
        )
        await session.execute(stmt)
    rows = (await session.execute(select(AppSetting))).scalars().all()
    return schemas.SettingsOut(values={r.key: r.value for r in rows})


# ---------------------------------------------------------------------------
# Export / import de la configuration (§9.5.10 — « ne jamais reperdre la config »)
# ---------------------------------------------------------------------------
@router.get("/config/export", response_model=schemas.ConfigExport)
async def export_config(session: AsyncSession = Depends(get_db)) -> schemas.ConfigExport:
    settings_rows = (await session.execute(select(AppSetting))).scalars().all()
    gpus = (await session.execute(select(Gpu).order_by(Gpu.id))).scalars().all()
    libraries = (await session.execute(select(Library).order_by(Library.id))).scalars().all()
    lib_dicts: list[dict[str, Any]] = []
    for lib in libraries:
        rules = (
            (
                await session.execute(
                    select(BitrateRule).where(BitrateRule.library_id == lib.id)
                )
            )
            .scalars()
            .all()
        )
        lib_dicts.append(
            {
                "name": lib.name,
                "path": lib.path,
                "enabled": lib.enabled,
                "paused": lib.paused,
                "output_codec": lib.output_codec.value,
                "container": lib.container.value,
                "hdr_policy": lib.hdr_policy.value,
                "dv_policy": lib.dv_policy.value,
                "date_filter_after": lib.date_filter_after.isoformat()
                if lib.date_filter_after
                else None,
                "priority": lib.priority,
                "include_extensions": lib.include_extensions,
                "exclude_globs": lib.exclude_globs,
                "min_gain_percent": float(lib.min_gain_percent),
                "scan_cron": lib.scan_cron,
                "dry_run": lib.dry_run,
                "bitrate_rules": [
                    {
                        "codec": r.codec.value,
                        "resolution_bucket": r.resolution_bucket.value,
                        "is_hdr": r.is_hdr,
                        "target_bitrate_kbps": r.target_bitrate_kbps,
                        "max_bitrate_kbps": r.max_bitrate_kbps,
                        "bufsize_kbps": r.bufsize_kbps,
                    }
                    for r in rules
                ],
            }
        )
    return schemas.ConfigExport(
        version=1,
        exported_at=datetime.datetime.now(tz=datetime.timezone.utc),
        settings={r.key: r.value for r in settings_rows},
        gpus=[
            {
                "name": g.name,
                "drm_node": g.drm_node,
                "api": g.api.value,
                "worker_count": g.worker_count,
                "enabled": g.enabled,
            }
            for g in gpus
        ],
        libraries=lib_dicts,
    )


@router.post("/config/import", response_model=schemas.ConfigImportResult)
async def import_config(
    payload: schemas.ConfigExport,
    session: AsyncSession = Depends(get_db),
    scheduler: ScanScheduler = Depends(get_scan_scheduler),
) -> schemas.ConfigImportResult:
    """Import transactionnel : upsert par nom/chemin, ne touche NI aux
    fichiers connus NI à l'historique des jobs."""
    if payload.version != 1:
        raise HTTPException(422, f"version d'export non supportée : {payload.version}")

    from transcoder.enums import Codec, Container, DvPolicy, GpuApi, HdrPolicy, ResolutionBucket
    import decimal

    n_settings = 0
    for key, value in payload.settings.items():
        if key not in _ALLOWED_SETTINGS:
            continue
        stmt = (
            pg_insert(AppSetting)
            .values(key=key, value=value)
            .on_conflict_do_update(index_elements=[AppSetting.key], set_={"value": value})
        )
        await session.execute(stmt)
        n_settings += 1

    n_gpus = 0
    for g in payload.gpus:
        existing_gpu = (
            await session.execute(select(Gpu).where(Gpu.drm_node == g.get("drm_node")))
        ).scalar_one_or_none()
        if existing_gpu is None:
            existing_gpu = Gpu(drm_node=str(g.get("drm_node")), name=str(g.get("name", "GPU")))
            session.add(existing_gpu)
        existing_gpu.name = str(g.get("name", existing_gpu.name))
        existing_gpu.api = GpuApi(str(g.get("api", "qsv")))
        existing_gpu.worker_count = int(g.get("worker_count", 1))
        existing_gpu.enabled = bool(g.get("enabled", True))
        n_gpus += 1

    n_libs = 0
    for entry in payload.libraries:
        lib = (
            await session.execute(select(Library).where(Library.path == entry.get("path")))
        ).scalar_one_or_none()
        if lib is None:
            lib = Library(name=str(entry["name"]), path=str(entry["path"]))
            session.add(lib)
        lib.name = str(entry.get("name", lib.name))
        lib.enabled = bool(entry.get("enabled", True))
        lib.paused = bool(entry.get("paused", False))
        lib.output_codec = Codec(str(entry.get("output_codec", "hevc")))
        lib.container = Container(str(entry.get("container", "mkv")))
        lib.hdr_policy = HdrPolicy(str(entry.get("hdr_policy", "preserve")))
        lib.dv_policy = DvPolicy(str(entry.get("dv_policy", "convert_to_hdr10")))
        raw_date = entry.get("date_filter_after")
        lib.date_filter_after = (
            datetime.datetime.fromisoformat(str(raw_date)) if raw_date else None
        )
        lib.priority = int(entry.get("priority", 0))
        lib.include_extensions = list(entry.get("include_extensions") or ["mkv", "mp4"])
        lib.exclude_globs = list(entry.get("exclude_globs") or [])
        lib.min_gain_percent = decimal.Decimal(f"{float(entry.get('min_gain_percent', 5.0)):.2f}")
        lib.scan_cron = entry.get("scan_cron")
        lib.dry_run = entry.get("dry_run")
        await session.flush()

        rules = entry.get("bitrate_rules")
        if isinstance(rules, list) and rules:
            existing_rules = (
                (
                    await session.execute(
                        select(BitrateRule).where(BitrateRule.library_id == lib.id)
                    )
                )
                .scalars()
                .all()
            )
            for r in existing_rules:
                await session.delete(r)
            await session.flush()
            for r in rules:
                session.add(
                    BitrateRule(
                        library_id=lib.id,
                        codec=Codec(str(r["codec"])),
                        resolution_bucket=ResolutionBucket(str(r["resolution_bucket"])),
                        is_hdr=bool(r["is_hdr"]),
                        target_bitrate_kbps=int(r["target_bitrate_kbps"]),
                        max_bitrate_kbps=int(r["max_bitrate_kbps"]),
                        bufsize_kbps=int(r["bufsize_kbps"]),
                    )
                )
        n_libs += 1
    await session.flush()
    await scheduler.reload()
    return schemas.ConfigImportResult(libraries=n_libs, gpus=n_gpus, settings=n_settings)


# ---------------------------------------------------------------------------
# Santé et capacités
# ---------------------------------------------------------------------------
def _read_build_info() -> tuple[str, str | None]:
    """(version, date de build) : fichiers posés par le Dockerfile, avec
    repli sur le fichier VERSION du repo en dev local."""
    import pathlib

    version = "dev"
    build_date: str | None = None
    candidates = [
        pathlib.Path("/srv/app/VERSION"),
        pathlib.Path(__file__).resolve().parents[4] / "VERSION",
    ]
    for candidate in candidates:
        try:
            version = candidate.read_text(encoding="utf-8").strip() or "dev"
            break
        except OSError:
            continue
    try:
        build_date = (
            pathlib.Path("/srv/app/BUILD_DATE").read_text(encoding="utf-8").strip() or None
        )
    except OSError:
        build_date = None
    return version, build_date


_APP_VERSION, _BUILD_DATE = _read_build_info()


@router.get("/health", response_model=schemas.HealthOut)
async def health(
    session: AsyncSession = Depends(get_db),
    qm: QueueManager = Depends(get_queue_manager),
) -> schemas.HealthOut:
    db_ok = True
    try:
        await session.execute(text("SELECT 1"))
    except Exception:
        db_ok = False
    config = await load_runtime_config(session)
    return schemas.HealthOut(
        status="ok" if db_ok else "degraded",
        database=db_ok,
        workers=qm.worker_count(),
        dry_run_global=config.dry_run_global,
        version=_APP_VERSION,
        build_date=_BUILD_DATE,
    )


@router.get("/capabilities")
async def capabilities_endpoint(
    caps: Capabilities = Depends(get_capabilities),
) -> dict[str, Any]:
    return caps.as_dict()
