"""Statistiques (§7.4) : KPIs, courbes, répartitions, top fichiers, échecs."""

from __future__ import annotations

import datetime
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import Numeric, case, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from transcoder.core.resources import sample_resources
from transcoder.db import get_db
from transcoder.models import Gpu as GpuModel
from transcoder.enums import JobState, MediaStatus
from transcoder.models import Job, Library, MediaFile

router = APIRouter(prefix="/stats", tags=["stats"])

_PROCESSED = (MediaStatus.DONE, MediaStatus.SKIPPED_NO_GAIN)


def _now() -> datetime.datetime:
    return datetime.datetime.now(tz=datetime.timezone.utc)


@router.get("/resources")
async def resources(session: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """CPU/RAM/GPU : consommation de l'app (cgroup) vs reste du système."""
    gpu_row = (
        await session.execute(select(GpuModel).where(GpuModel.enabled.is_(True)).limit(1))
    ).scalar_one_or_none()
    drm_node = gpu_row.drm_node if gpu_row is not None else "/dev/dri/renderD128"
    return await sample_resources(drm_node)


@router.get("/summary")
async def summary(session: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """KPIs du dashboard : traités/total, Go récupérés, gain moyen, débit, ETA."""
    total_files = (await session.scalar(select(func.count()).select_from(MediaFile))) or 0
    by_status_rows = (
        await session.execute(
            select(MediaFile.status, func.count()).group_by(MediaFile.status)
        )
    ).all()
    by_status = {status.value: count for status, count in by_status_rows}
    processed = sum(by_status.get(s.value, 0) for s in _PROCESSED)

    bytes_saved = (
        await session.scalar(
            select(func.coalesce(func.sum(Job.bytes_saved), 0)).where(
                Job.state == JobState.SUCCESS, Job.dry_run.is_(False)
            )
        )
    ) or 0
    avg_gain = await session.scalar(
        select(func.avg(Job.gain_percent)).where(Job.state == JobState.SUCCESS)
    )

    day_ago = _now() - datetime.timedelta(hours=24)
    done_24h = (
        await session.scalar(
            select(func.count())
            .select_from(Job)
            .where(Job.state == JobState.SUCCESS, Job.finished_at >= day_ago)
        )
    ) or 0
    avg_job_s = await session.scalar(
        select(func.avg(Job.duration_s)).where(
            Job.state == JobState.SUCCESS, Job.finished_at >= day_ago
        )
    )
    queued = by_status.get(MediaStatus.QUEUED.value, 0)
    eta_s: float | None = None
    if avg_job_s and queued:
        eta_s = float(avg_job_s) * queued

    dry_run_libs = (
        await session.execute(select(Library.id, Library.name, Library.dry_run))
    ).all()
    return {
        "total_files": total_files,
        "processed": processed,
        "by_status": by_status,
        "bytes_saved": int(bytes_saved),
        "avg_gain_percent": float(avg_gain) if avg_gain is not None else None,
        "throughput_files_per_hour": done_24h / 24.0,
        "eta_s": eta_s,
        "queued": queued,
        "libraries_dry_run": [
            {"id": i, "name": n, "dry_run": d} for i, n, d in dry_run_libs
        ],
    }


@router.get("/savings-over-time")
async def savings_over_time(
    session: AsyncSession = Depends(get_db),
    days: int = Query(default=90, ge=1, le=730),
) -> list[dict[str, Any]]:
    """Courbe cumulative de l'espace récupéré (§7.4)."""
    since = _now() - datetime.timedelta(days=days)
    day = func.date_trunc("day", Job.finished_at)
    rows = (
        await session.execute(
            select(day.label("day"), func.sum(Job.bytes_saved))
            .where(
                Job.state == JobState.SUCCESS,
                Job.dry_run.is_(False),
                Job.finished_at >= since,
                Job.bytes_saved.is_not(None),
            )
            .group_by(day)
            .order_by(day)
        )
    ).all()
    cumulative = 0
    out: list[dict[str, Any]] = []
    for d, saved in rows:
        cumulative += int(saved or 0)
        out.append({"day": d.isoformat(), "bytes_saved": int(saved or 0), "cumulative": cumulative})
    return out


@router.get("/codecs")
async def codecs(session: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """Répartition des codecs avant/après (donut §7.4)."""
    current_rows = (
        await session.execute(
            select(MediaFile.video_codec, func.count())
            .where(MediaFile.video_codec.is_not(None))
            .group_by(MediaFile.video_codec)
        )
    ).all()
    before_rows = (
        await session.execute(
            select(Job.src_codec, func.count())
            .where(Job.state == JobState.SUCCESS, Job.src_codec.is_not(None))
            .group_by(Job.src_codec)
        )
    ).all()
    return {
        "current": {c: n for c, n in current_rows},
        "replaced_sources": {c: n for c, n in before_rows},
    }


@router.get("/gain-by-bucket")
async def gain_by_bucket(session: AsyncSession = Depends(get_db)) -> list[dict[str, Any]]:
    """Gain moyen par BUCKET de résolution — pas par résolution brute : un
    1920x804 cinémascope est du 1080p, sinon chaque crop créerait son
    propre groupe d'un seul fichier."""
    from transcoder.core.buckets import bucket_for_height

    rows = (
        await session.execute(
            select(Job.src_resolution, Job.dst_codec, Job.gain_percent).where(
                Job.state == JobState.SUCCESS, Job.gain_percent.is_not(None)
            )
        )
    ).all()
    agg: dict[tuple[str, str], tuple[float, int]] = {}
    for res, codec, gain in rows:
        if not res or "x" not in res or gain is None:
            continue
        try:
            height = int(res.split("x")[1])
        except (ValueError, IndexError):
            continue
        key = (bucket_for_height(height).value, codec or "?")
        total, count = agg.get(key, (0.0, 0))
        agg[key] = (total + float(gain), count + 1)
    bucket_order = {"2160p": 0, "1440p": 1, "1080p": 2, "720p": 3, "576p": 4, "480p": 5}
    return [
        {
            "resolution": bucket,
            "codec": codec,
            "avg_gain_percent": round(total / count, 2),
            "count": count,
        }
        for (bucket, codec), (total, count) in sorted(
            agg.items(), key=lambda kv: (bucket_order.get(kv[0][0], 9), kv[0][1])
        )
    ]


@router.get("/throughput")
async def throughput(
    session: AsyncSession = Depends(get_db),
    days: int = Query(default=30, ge=1, le=365),
) -> dict[str, Any]:
    since = _now() - datetime.timedelta(days=days)
    day = func.date_trunc("day", Job.finished_at)
    per_day = (
        await session.execute(
            select(day.label("d"), func.count(), func.sum(Job.duration_s))
            .where(Job.state == JobState.SUCCESS, Job.finished_at >= since)
            .group_by(day)
            .order_by(day)
        )
    ).all()
    by_gpu = (
        await session.execute(
            select(
                Job.gpu_id,
                func.count(),
                func.avg(Job.avg_fps),
                func.sum(Job.duration_s),
            )
            .where(Job.state == JobState.SUCCESS, Job.finished_at >= since)
            .group_by(Job.gpu_id)
        )
    ).all()
    return {
        "per_day": [
            {
                "day": d.isoformat(),
                "files": n,
                "gpu_hours": float(dur or 0) / 3600.0,
            }
            for d, n, dur in per_day
        ],
        "by_gpu": [
            {
                "gpu_id": g,
                "files": n,
                "avg_fps": float(fps) if fps is not None else None,
                "gpu_hours": float(dur or 0) / 3600.0,
            }
            for g, n, fps, dur in by_gpu
        ],
    }


@router.get("/top-remaining")
async def top_remaining(
    session: AsyncSession = Depends(get_db),
    limit: int = Query(default=20, ge=1, le=100),
) -> list[dict[str, Any]]:
    """Top des fichiers les plus lourds encore non traités (§7.4)."""
    rows = (
        await session.execute(
            select(MediaFile)
            .where(MediaFile.status.in_((MediaStatus.PENDING, MediaStatus.QUEUED)))
            .order_by(MediaFile.size_bytes.desc().nulls_last())
            .limit(limit)
        )
    ).scalars()
    return [
        {
            "id": m.id,
            "path": m.path,
            "library_id": m.library_id,
            "size_bytes": m.size_bytes,
            "video_codec": m.video_codec,
            "resolution_bucket": m.resolution_bucket.value if m.resolution_bucket else None,
            "estimated_gain_bytes": m.estimated_gain_bytes,
            "status": m.status.value,
        }
        for m in rows
    ]


@router.get("/failures")
async def failures(session: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    total_jobs = (await session.scalar(select(func.count()).select_from(Job))) or 0
    failed_jobs = (
        await session.scalar(
            select(func.count()).select_from(Job).where(Job.state == JobState.FAILED)
        )
    ) or 0
    top_errors = (
        await session.execute(
            select(Job.error_message, func.count())
            .where(Job.state == JobState.FAILED, Job.error_message.is_not(None))
            .group_by(Job.error_message)
            .order_by(func.count().desc())
            .limit(15)
        )
    ).all()
    return {
        "total_jobs": total_jobs,
        "failed_jobs": failed_jobs,
        "failure_rate": (failed_jobs / total_jobs) if total_jobs else 0.0,
        "top_errors": [{"error": e, "count": n} for e, n in top_errors],
    }


@router.get("/progress-by-library")
async def progress_by_library(session: AsyncSession = Depends(get_db)) -> list[dict[str, Any]]:
    """% de fichiers conformes (bon codec ET bon bitrate) par bibliothèque."""
    conform = func.sum(
        case((MediaFile.status.in_(_PROCESSED), 1), else_=0)
    )
    rows = (
        await session.execute(
            select(
                Library.id,
                Library.name,
                func.count(MediaFile.id),
                cast(conform, Numeric),
            )
            .join(MediaFile, MediaFile.library_id == Library.id, isouter=True)
            .group_by(Library.id, Library.name)
            .order_by(Library.id)
        )
    ).all()
    return [
        {
            "library_id": lid,
            "name": name,
            "total": total,
            "conform": int(conf or 0),
            "percent": (float(conf or 0) / total * 100.0) if total else 0.0,
        }
        for lid, name, total, conf in rows
    ]
