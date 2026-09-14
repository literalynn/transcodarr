"""Statistiques (§7.4) : KPIs, courbes, répartitions, top fichiers, échecs.

Les statistiques d'activité (tentatives, échecs, espace récupéré, débit,
gain par bucket) acceptent une période `days` (absente = depuis toujours).
Les périodes sont alignées sur l'heure (24 h) ou le jour UTC, et les
courbes renvoient TOUS les créneaux de la période, vides compris : l'axe
du temps reste régulier et les cartes du dashboard concordent."""

from __future__ import annotations

import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy import Numeric, case, cast, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from transcoder.api.deps import get_queue_manager
from transcoder.core.queue import QueueManager
from transcoder.core.resources import sample_resources
from transcoder.db import get_db
from transcoder.enums import JobState, MediaStatus
from transcoder.models import Gpu as GpuModel
from transcoder.models import Job, Library, MediaFile

router = APIRouter(prefix="/stats", tags=["stats"])

_PROCESSED = (MediaStatus.DONE, MediaStatus.SKIPPED_NO_GAIN)
_UTC = datetime.timezone.utc

TimeUnit = Literal["hour", "day", "week"]
_STEPS: dict[str, datetime.timedelta] = {
    "hour": datetime.timedelta(hours=1),
    "day": datetime.timedelta(days=1),
    "week": datetime.timedelta(weeks=1),
}
# Au-delà de ce nombre de jours, une courbe « depuis toujours » passe à la semaine
_MAX_DAILY_SPAN_DAYS = 92
# Période en jours acceptée par les endpoints (absente = depuis toujours)
_DAYS_QUERY = Query(default=None, ge=1, le=3650)


def _now() -> datetime.datetime:
    return datetime.datetime.now(tz=_UTC)


def truncate(moment: datetime.datetime, unit: TimeUnit) -> datetime.datetime:
    """Début du créneau UTC contenant `moment` (même découpage que date_trunc)."""
    moment = moment.astimezone(_UTC)
    if unit == "hour":
        return moment.replace(minute=0, second=0, microsecond=0)
    day = moment.replace(hour=0, minute=0, second=0, microsecond=0)
    if unit == "day":
        return day
    return day - datetime.timedelta(days=day.weekday())  # semaine ISO : lundi


def period_start(days: int | None, now: datetime.datetime) -> datetime.datetime | None:
    """Début de période aligné : 24 créneaux horaires pour 1 jour, sinon
    `days` jours calendaires UTC (aujourd'hui compris). None = depuis toujours."""
    if days is None:
        return None
    if days == 1:
        return truncate(now, "hour") - datetime.timedelta(hours=23)
    return truncate(now, "day") - datetime.timedelta(days=days - 1)


def timeline_unit(
    days: int | None, first: datetime.datetime | None, now: datetime.datetime
) -> TimeUnit:
    """Granularité des courbes : heure sur 24 h, jour, semaine au-delà de 3 mois."""
    if days is not None:
        return "hour" if days == 1 else ("day" if days <= _MAX_DAILY_SPAN_DAYS else "week")
    span = (now - first) if first is not None else datetime.timedelta(0)
    if span <= datetime.timedelta(days=2):
        return "hour"
    return "day" if span <= datetime.timedelta(days=_MAX_DAILY_SPAN_DAYS) else "week"


def bucket_starts(
    start: datetime.datetime, now: datetime.datetime, unit: TimeUnit
) -> list[datetime.datetime]:
    """Tous les créneaux de `start` à `now` inclus (pur)."""
    current, last = truncate(start, unit), truncate(now, unit)
    out: list[datetime.datetime] = []
    while current <= last:
        out.append(current)
        current += _STEPS[unit]
    return out


def _bucket_expr(unit: TimeUnit) -> ColumnElement[datetime.datetime]:
    """Créneau d'un job en SQL, en UTC quel que soit le fuseau de la session."""
    return func.date_trunc(unit, func.timezone("UTC", Job.finished_at))


def _bucket_key(value: datetime.datetime) -> datetime.datetime:
    """Clé de créneau renvoyée par Postgres (naïve UTC) → datetime UTC."""
    return value.replace(tzinfo=_UTC) if value.tzinfo is None else value.astimezone(_UTC)


async def _timeline(
    session: AsyncSession, days: int | None, conditions: list[Any]
) -> tuple[TimeUnit, list[datetime.datetime]]:
    """Granularité et créneaux d'une courbe sur la période."""
    now = _now()
    since = period_start(days, now)
    first = since
    if first is None:
        first = await session.scalar(select(func.min(Job.finished_at)).where(*conditions))
    unit = timeline_unit(days, first, now)
    return unit, (bucket_starts(first, now, unit) if first is not None else [])


def _period_conditions(days: int | None) -> list[Any]:
    since = period_start(days, _now())
    return [Job.finished_at >= since] if since is not None else [Job.finished_at.is_not(None)]


@router.get("/resources")
async def resources(session: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """CPU/RAM/GPU : consommation de l'app (cgroup) vs reste du système."""
    gpu_row = (
        await session.execute(select(GpuModel).where(GpuModel.enabled.is_(True)).limit(1))
    ).scalar_one_or_none()
    drm_node = gpu_row.drm_node if gpu_row is not None else "/dev/dri/renderD128"
    return await sample_resources(drm_node)


@router.get("/summary")
async def summary(
    session: AsyncSession = Depends(get_db),
    qm: QueueManager = Depends(get_queue_manager),
) -> dict[str, Any]:
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
    # Durée moyenne des 50 derniers encodages réels (réussis ou sans gain),
    # indépendante des périodes d'inactivité de la file
    recent = (
        select(Job.duration_s)
        .where(
            Job.state.in_((JobState.SUCCESS, JobState.SKIPPED_NO_GAIN)),
            Job.duration_s > 0,
            Job.finished_at.is_not(None),
        )
        .order_by(Job.finished_at.desc())
        .limit(50)
        .subquery()
    )
    avg_job_s = await session.scalar(select(func.avg(recent.c.duration_s)))
    queued = by_status.get(MediaStatus.QUEUED.value, 0)
    eta_s: float | None = None
    if avg_job_s and queued:
        # Les workers encodent en parallèle
        eta_s = float(avg_job_s) * queued / max(1, qm.worker_count())

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
    days: int | None = _DAYS_QUERY,
) -> dict[str, Any]:
    """Espace récupéré par créneau et cumulé sur la période (§7.4)."""
    conditions = [
        Job.state == JobState.SUCCESS,
        Job.dry_run.is_(False),
        Job.bytes_saved.is_not(None),
        *_period_conditions(days),
    ]
    unit, buckets = await _timeline(session, days, conditions)
    bucket = _bucket_expr(unit)
    rows = (
        await session.execute(
            select(bucket, func.sum(Job.bytes_saved), func.count())
            .where(*conditions)
            .group_by(bucket)
        )
    ).all()
    by_bucket = {_bucket_key(b): (int(saved or 0), int(n)) for b, saved, n in rows}
    files = sum(n for _, n in by_bucket.values())
    avg_gain = await session.scalar(select(func.avg(Job.gain_percent)).where(*conditions))
    cumulative = 0
    points: list[dict[str, Any]] = []
    for start in buckets:
        saved, _ = by_bucket.get(start, (0, 0))
        cumulative += saved
        points.append({"t": start.isoformat(), "bytes_saved": saved, "cumulative": cumulative})
    return {
        "period_days": days,
        "unit": unit,
        "total_bytes": cumulative,
        "files": files,
        "avg_gain_percent": float(avg_gain) if avg_gain is not None else None,
        "points": points,
    }


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
async def gain_by_bucket(
    session: AsyncSession = Depends(get_db),
    days: int | None = _DAYS_QUERY,
) -> list[dict[str, Any]]:
    """Gain moyen par BUCKET de résolution — pas par résolution brute : un
    1920x804 cinémascope est du 1080p, sinon chaque crop créerait son
    propre groupe d'un seul fichier."""
    from transcoder.core.buckets import bucket_for_height

    rows = (
        await session.execute(
            select(Job.src_resolution, Job.dst_codec, Job.gain_percent).where(
                Job.state == JobState.SUCCESS,
                Job.gain_percent.is_not(None),
                *_period_conditions(days),
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
    days: int | None = _DAYS_QUERY,
) -> dict[str, Any]:
    """Fichiers réencodés avec succès par créneau, temps GPU et débit moyen."""
    conditions = [Job.state == JobState.SUCCESS, *_period_conditions(days)]
    unit, buckets = await _timeline(session, days, conditions)
    bucket = _bucket_expr(unit)
    rows = (
        await session.execute(
            select(bucket, func.count(), func.sum(Job.duration_s))
            .where(*conditions)
            .group_by(bucket)
        )
    ).all()
    by_bucket = {_bucket_key(b): (int(n), float(dur or 0)) for b, n, dur in rows}
    by_gpu = (
        await session.execute(
            select(
                Job.gpu_id,
                func.count(),
                func.avg(Job.avg_fps),
                func.sum(Job.duration_s),
            )
            .where(*conditions)
            .group_by(Job.gpu_id)
        )
    ).all()
    files = sum(n for n, _ in by_bucket.values())
    # Débit moyen sur la durée écoulée de la période (depuis le 1er créneau)
    elapsed_h = (
        max(1.0, (_now() - buckets[0]).total_seconds() / 3600.0) if buckets else None
    )
    return {
        "period_days": days,
        "unit": unit,
        "files": files,
        "gpu_hours": round(sum(d for _, d in by_bucket.values()) / 3600.0, 2),
        "files_per_hour": (files / elapsed_h) if elapsed_h else None,
        "points": [
            {
                "t": start.isoformat(),
                "files": by_bucket.get(start, (0, 0.0))[0],
                "gpu_hours": round(by_bucket.get(start, (0, 0.0))[1] / 3600.0, 3),
            }
            for start in buckets
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
async def failures(
    session: AsyncSession = Depends(get_db),
    days: int | None = _DAYS_QUERY,
) -> dict[str, Any]:
    """Échecs par catégorie : tentatives (période) et fichiers en échec définitif."""
    job_filter = _period_conditions(days)
    total_jobs = (
        await session.scalar(select(func.count()).select_from(Job).where(*job_filter))
    ) or 0
    failed_jobs = (
        await session.scalar(
            select(func.count())
            .select_from(Job)
            .where(Job.state == JobState.FAILED, *job_filter)
        )
    ) or 0
    by_category = (
        await session.execute(
            select(
                Job.failure_category,
                func.count(),
                func.count(func.distinct(Job.media_file_id)),
                func.max(Job.finished_at),
            )
            .where(Job.state == JobState.FAILED, *job_filter)
            .group_by(Job.failure_category)
            .order_by(func.count().desc())
        )
    ).all()
    failed_files: dict[str | None, int] = {
        category: count
        for category, count in (
            await session.execute(
                select(MediaFile.failure_category, func.count())
                .where(MediaFile.status == MediaStatus.FAILED)
                .group_by(MediaFile.failure_category)
            )
        ).all()
    }
    top_errors = (
        await session.execute(
            select(Job.error_message, Job.failure_category, func.count())
            .where(Job.state == JobState.FAILED, Job.error_message.is_not(None), *job_filter)
            .group_by(Job.error_message, Job.failure_category)
            .order_by(func.count().desc())
            .limit(15)
        )
    ).all()
    categories = {c for c, *_ in by_category} | set(failed_files)
    rows_by_cat = {c: (n, f, last) for c, n, f, last in by_category}
    return {
        "period_days": days,
        "total_jobs": total_jobs,
        "failed_jobs": failed_jobs,
        "failure_rate": (failed_jobs / total_jobs) if total_jobs else 0.0,
        "failed_files_total": sum(failed_files.values()),
        "categories": sorted(
            (
                {
                    "category": c or "unknown",
                    "jobs": rows_by_cat.get(c, (0, 0, None))[0],
                    "files": rows_by_cat.get(c, (0, 0, None))[1],
                    "failed_files": failed_files.get(c, 0),
                    "last_seen": (
                        rows_by_cat[c][2].isoformat()
                        if c in rows_by_cat and rows_by_cat[c][2]
                        else None
                    ),
                }
                for c in categories
            ),
            key=lambda item: (-item["failed_files"], -item["jobs"]),
        ),
        "top_errors": [
            {"error": e, "category": cat, "count": n} for e, cat, n in top_errors
        ],
    }


@router.get("/reliability")
async def reliability(
    session: AsyncSession = Depends(get_db),
    days: int | None = _DAYS_QUERY,
) -> dict[str, Any]:
    """Fiabilité : tentatives vs réussites, premier essai, replis, temps perdu.

    « Tentative » = job ayant réellement lancé un encodage (les évaluations
    du pré-vol, sans GPU, sont comptées à part)."""
    job_filter = _period_conditions(days)
    preflight = Job.strategy == "preflight"
    # Expressions réutilisées À L'IDENTIQUE dans SELECT et GROUP BY
    is_preflight = func.coalesce(preflight, False)
    is_early = func.coalesce(Job.error_message.like("abandon anticipé%"), False)

    rows = (
        await session.execute(
            select(
                Job.state,
                is_preflight,
                is_early,
                func.count(),
                func.coalesce(func.sum(Job.duration_s), 0),
                func.count(func.distinct(Job.media_file_id)),
            )
            .where(*job_filter)
            .group_by(Job.state, is_preflight, is_early)
        )
    ).all()
    counts = {"success": 0, "failed": 0, "no_gain": 0, "no_gain_early": 0,
              "no_gain_preflight": 0, "cancelled": 0}
    gpu_s = {"success": 0.0, "failed": 0.0, "no_gain": 0.0, "cancelled": 0.0}
    for state, is_preflight, is_early, n, dur, _files in rows:
        if state is JobState.SUCCESS:
            counts["success"] += n
            gpu_s["success"] += float(dur)
        elif state is JobState.FAILED:
            counts["failed"] += n
            gpu_s["failed"] += float(dur)
        elif state is JobState.SKIPPED_NO_GAIN:
            if is_preflight:
                counts["no_gain_preflight"] += n
            else:
                counts["no_gain"] += n
                gpu_s["no_gain"] += float(dur)
                if is_early:
                    counts["no_gain_early"] += n
        elif state is JobState.CANCELLED:
            if not is_preflight:
                counts["cancelled"] += n
                gpu_s["cancelled"] += float(dur)
    attempts = counts["success"] + counts["failed"] + counts["no_gain"]

    first_try = (
        await session.scalar(
            select(func.count())
            .select_from(Job)
            .where(Job.state == JobState.SUCCESS, Job.attempt <= 1, *job_filter)
        )
    ) or 0
    recovered = counts["success"] - first_try

    # Fichiers : issue de chaque fichier tenté sur la période
    file_outcomes = (
        await session.execute(
            select(
                func.count(func.distinct(Job.media_file_id)),
                func.count(func.distinct(Job.media_file_id)).filter(Job.state == JobState.SUCCESS),
                func.count(func.distinct(Job.media_file_id)).filter(
                    Job.state == JobState.SKIPPED_NO_GAIN
                ),
            ).where(*job_filter, or_(preflight.is_(False), Job.strategy.is_(None)))
        )
    ).one()
    failed_now = (
        await session.scalar(
            select(func.count())
            .select_from(MediaFile)
            .where(MediaFile.status == MediaStatus.FAILED)
        )
    ) or 0
    retrying_now = (
        await session.scalar(
            select(func.count())
            .select_from(MediaFile)
            .where(MediaFile.status == MediaStatus.QUEUED, MediaFile.attempt_count > 0)
        )
    ) or 0
    predicted_no_gain = (
        await session.scalar(
            select(func.count())
            .select_from(MediaFile)
            .where(
                MediaFile.status == MediaStatus.SKIPPED_NO_GAIN,
                MediaFile.last_error.like("gain prévu insuffisant%"),
            )
        )
    ) or 0

    attempt_filter = [
        *job_filter,
        or_(preflight.is_(False), Job.strategy.is_(None)),
        Job.state.in_((JobState.SUCCESS, JobState.FAILED, JobState.SKIPPED_NO_GAIN)),
    ]
    unit, buckets = await _timeline(session, days, attempt_filter)
    bucket = _bucket_expr(unit)
    timeline_rows = (
        await session.execute(
            select(bucket, Job.state, func.count())
            .where(*attempt_filter)
            .group_by(bucket, Job.state)
        )
    ).all()
    timeline: dict[datetime.datetime, dict[str, Any]] = {
        start: {"t": start.isoformat(), "success": 0, "failed": 0, "no_gain": 0}
        for start in buckets
    }
    for b, state, n in timeline_rows:
        entry = timeline.get(_bucket_key(b))
        if entry is not None:
            entry["no_gain" if state is JobState.SKIPPED_NO_GAIN else state.value] += n

    strategy_rows = (
        await session.execute(
            select(Job.strategy, Job.state, func.count())
            .where(*job_filter, Job.strategy.is_not(None), preflight.is_(False))
            .where(Job.state.in_((JobState.SUCCESS, JobState.FAILED, JobState.SKIPPED_NO_GAIN)))
            .group_by(Job.strategy, Job.state)
        )
    ).all()
    strategies: dict[str, dict[str, Any]] = {}
    for strategy, state, n in strategy_rows:
        entry = strategies.setdefault(
            strategy, {"strategy": strategy, "success": 0, "failed": 0, "no_gain": 0}
        )
        entry["no_gain" if state is JobState.SKIPPED_NO_GAIN else state.value] += n

    def _rate(num: int, den: int) -> float | None:
        return num / den if den else None

    return {
        "period_days": days,
        "attempts": attempts,
        "counts": counts,
        "success_rate": _rate(counts["success"], counts["success"] + counts["failed"]),
        "attempt_success_rate": _rate(counts["success"], attempts),
        "first_try_success": first_try,
        "first_try_rate": _rate(first_try, counts["success"]),
        "recovered_by_fallback": recovered,
        "files": {
            "attempted": file_outcomes[0],
            "succeeded": file_outcomes[1],
            "no_gain": file_outcomes[2],
            "failed_now": failed_now,
            "retrying_now": retrying_now,
            "predicted_no_gain": predicted_no_gain,
        },
        "gpu_hours": {k: round(v / 3600.0, 2) for k, v in gpu_s.items()},
        "unit": unit,
        "timeline": list(timeline.values()),
        "strategies": sorted(strategies.values(), key=lambda e: e["strategy"]),
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
