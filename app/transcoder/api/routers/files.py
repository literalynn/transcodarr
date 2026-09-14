"""Explorateur des fichiers : état par fichier, force/ignore/reset (§6)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from transcoder.api import schemas
from transcoder.api.deps import get_capabilities
from transcoder.core.capabilities import Capabilities
from transcoder.core.queue import (
    requeue_dry_run_files,
    reset_for_retry,
    retry_failed_files,
    review_estimated_no_gain,
)
from transcoder.db import get_db
from transcoder.enums import MediaStatus
from transcoder.models import Job, MediaFile

router = APIRouter(prefix="/files", tags=["files"])


async def _job_counts(session: AsyncSession, file_ids: list[int]) -> dict[int, int]:
    if not file_ids:
        return {}
    rows = await session.execute(
        select(Job.media_file_id, func.count())
        .where(Job.media_file_id.in_(file_ids))
        .group_by(Job.media_file_id)
    )
    return {fid: n for fid, n in rows.all()}


@router.get("", response_model=schemas.MediaFilePage)
async def list_files(
    session: AsyncSession = Depends(get_db),
    library_id: int | None = None,
    status: MediaStatus | None = None,
    failure_category: str | None = None,
    search: str | None = None,
    order: str = Query(default="queue", pattern="^(queue|path|size|mtime|recent)$"),
    with_job_count: bool = False,
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
) -> schemas.MediaFilePage:
    stmt = select(MediaFile)
    count_stmt = select(func.count()).select_from(MediaFile)
    conditions = []
    if library_id is not None:
        conditions.append(MediaFile.library_id == library_id)
    if status is not None:
        conditions.append(MediaFile.status == status)
    if failure_category:
        conditions.append(MediaFile.failure_category == failure_category)
    if search:
        conditions.append(MediaFile.path.ilike(f"%{search}%"))
    if conditions:
        stmt = stmt.where(*conditions)
        count_stmt = count_stmt.where(*conditions)
    if order == "queue":
        # Même ordre que le claim des workers (§5.2) : ce que l'onglet
        # « En attente » affiche est ce qui partira en premier
        stmt = stmt.order_by(
            MediaFile.manual_rank.asc().nulls_last(),
            MediaFile.estimated_gain_bytes.desc().nulls_last(),
            MediaFile.mtime.desc(),
        )
    elif order == "path":
        stmt = stmt.order_by(MediaFile.path)
    elif order == "size":
        stmt = stmt.order_by(MediaFile.size_bytes.desc().nulls_last())
    elif order == "recent":
        stmt = stmt.order_by(MediaFile.id.desc())
    else:
        stmt = stmt.order_by(MediaFile.mtime.desc().nulls_last())
    total = (await session.scalar(count_stmt)) or 0
    rows = (await session.execute(stmt.limit(limit).offset(offset))).scalars().all()
    counts = await _job_counts(session, [r.id for r in rows]) if with_job_count else {}
    items = [
        schemas.MediaFileOut.model_validate(r).model_copy(
            update={"job_count": counts.get(r.id, 0)} if with_job_count else {}
        )
        for r in rows
    ]
    return schemas.MediaFilePage(
        items=items,
        meta=schemas.PageMeta(total=total, limit=limit, offset=offset),
    )


@router.post("/retry-failed", response_model=schemas.ActionResult)
async def retry_failed(
    payload: schemas.RetryFailedRequest, session: AsyncSession = Depends(get_db)
) -> schemas.ActionResult:
    """Relance les échecs définitifs avec toutes les méthodes actuelles
    (plan de pistes, pré-vol, replis automatiques) : nouvelle série complète."""
    count = await retry_failed_files(
        session,
        library_id=payload.library_id,
        failure_category=payload.failure_category,
        file_ids=payload.file_ids,
    )
    return schemas.ActionResult(ok=True, detail=f"{count} fichier(s) en échec remis en file")


@router.post("/requeue-no-gain", response_model=schemas.NoGainReviewResult)
async def requeue_no_gain(
    payload: schemas.NoGainReviewRequest,
    session: AsyncSession = Depends(get_db),
    caps: Capabilities = Depends(get_capabilities),
) -> schemas.NoGainReviewResult:
    """Remet en file les « sans gain » jamais mesurés (abandon anticipé,
    prédiction) dont la prédiction actuelle laisse espérer un gain ; les
    gains mesurés après encodage complet sont conservés."""
    review = await review_estimated_no_gain(
        session, dv_caps=caps.dv_caps, library_id=payload.library_id, apply=not payload.dry_run
    )
    verb = "seraient remis" if payload.dry_run else "remis"
    detail = (
        f"{review.requeued} fichier(s) {verb} en file ; {review.still_hopeless} confirmé(s) "
        f"sans gain par la prédiction ; {review.measured_kept} gain(s) mesuré(s) conservé(s)"
    )
    return schemas.NoGainReviewResult(dry_run=payload.dry_run, detail=detail, **review.as_dict())


@router.post("/requeue-dry-run", response_model=schemas.ActionResult)
async def requeue_dry_run(
    library_id: int | None = None, session: AsyncSession = Depends(get_db)
) -> schemas.ActionResult:
    """Re-queue tous les fichiers traités uniquement en dry-run (§9.4) :
    à utiliser après passage en réel, sans attendre le prochain scan."""
    count = await requeue_dry_run_files(session, library_id)
    return schemas.ActionResult(ok=True, detail=f"{count} fichier(s) dry-run remis en queue")


async def _get_file(session: AsyncSession, file_id: int) -> MediaFile:
    mf = await session.get(MediaFile, file_id)
    if mf is None:
        raise HTTPException(404, "fichier introuvable")
    return mf


@router.get("/{file_id}", response_model=schemas.MediaFileDetail)
async def file_detail(
    file_id: int, session: AsyncSession = Depends(get_db)
) -> schemas.MediaFileDetail:
    """Fichier + historique complet de ses tentatives (plus récente d'abord)."""
    mf = await _get_file(session, file_id)
    jobs = (
        (
            await session.execute(
                select(Job).where(Job.media_file_id == file_id).order_by(Job.id.desc()).limit(200)
            )
        )
        .scalars()
        .all()
    )
    base = schemas.MediaFileOut.model_validate(mf).model_dump()
    base["job_count"] = len(jobs)
    return schemas.MediaFileDetail(
        **base,
        jobs=[schemas.JobOut.model_validate(j) for j in jobs],
        audio_streams=mf.audio_streams,
        subtitle_streams=mf.subtitle_streams,
    )


@router.post("/{file_id}/force", response_model=schemas.ActionResult)
async def force_file(file_id: int, session: AsyncSession = Depends(get_db)) -> schemas.ActionResult:
    """« Forcer le réencodage » (§5.7) : tête de queue, compteurs remis à zéro."""
    mf = await _get_file(session, file_id)
    if mf.status is MediaStatus.PROCESSING:
        raise HTTPException(409, "fichier en cours de traitement")
    if mf.status is MediaStatus.QUEUED:
        # Déjà en file (éventuellement au milieu d'une série de replis) :
        # simple priorisation, la stratégie prévue est conservée
        mf.next_attempt_at = None
    else:
        reset_for_retry(mf)
    # manual_rank = 0 : tête de queue ET marqueur « forcé » (le pré-vol
    # n'écarte pas le fichier sur une prédiction de gain insuffisant)
    mf.manual_rank = 0
    return schemas.ActionResult(ok=True, detail="réencodage forcé, fichier en tête de queue")


@router.post("/{file_id}/ignore", response_model=schemas.ActionResult)
async def ignore_file(file_id: int, session: AsyncSession = Depends(get_db)) -> schemas.ActionResult:
    mf = await _get_file(session, file_id)
    if mf.status is MediaStatus.PROCESSING:
        raise HTTPException(409, "fichier en cours de traitement : annuler le job d'abord")
    mf.status = MediaStatus.IGNORED
    mf.manual_rank = None
    return schemas.ActionResult(ok=True, detail="fichier ignoré (plus jamais traité)")


@router.post("/{file_id}/reset", response_model=schemas.ActionResult)
async def reset_file(file_id: int, session: AsyncSession = Depends(get_db)) -> schemas.ActionResult:
    """Réinitialise un fichier failed/ignored : re-décision au prochain scan."""
    mf = await _get_file(session, file_id)
    if mf.status is MediaStatus.PROCESSING:
        raise HTTPException(409, "fichier en cours de traitement")
    reset_for_retry(mf)
    mf.status = MediaStatus.PENDING
    return schemas.ActionResult(ok=True, detail="fichier réinitialisé")
