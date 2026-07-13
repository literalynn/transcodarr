"""Explorateur des fichiers : état par fichier, force/ignore/reset (§6)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from transcoder.api import schemas
from transcoder.db import get_db
from transcoder.enums import MediaStatus
from transcoder.models import MediaFile

router = APIRouter(prefix="/files", tags=["files"])


@router.get("", response_model=schemas.MediaFilePage)
async def list_files(
    session: AsyncSession = Depends(get_db),
    library_id: int | None = None,
    status: MediaStatus | None = None,
    search: str | None = None,
    order: str = Query(default="queue", pattern="^(queue|path|size|mtime)$"),
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
    else:
        stmt = stmt.order_by(MediaFile.mtime.desc().nulls_last())
    total = (await session.scalar(count_stmt)) or 0
    rows = (await session.execute(stmt.limit(limit).offset(offset))).scalars().all()
    return schemas.MediaFilePage(
        items=[schemas.MediaFileOut.model_validate(r) for r in rows],
        meta=schemas.PageMeta(total=total, limit=limit, offset=offset),
    )


async def _get_file(session: AsyncSession, file_id: int) -> MediaFile:
    mf = await session.get(MediaFile, file_id)
    if mf is None:
        raise HTTPException(404, "fichier introuvable")
    return mf


@router.post("/{file_id}/force", response_model=schemas.ActionResult)
async def force_file(file_id: int, session: AsyncSession = Depends(get_db)) -> schemas.ActionResult:
    """« Forcer le réencodage » (§5.7) : tête de queue, compteurs remis à zéro."""
    mf = await _get_file(session, file_id)
    if mf.status is MediaStatus.PROCESSING:
        raise HTTPException(409, "fichier en cours de traitement")
    mf.status = MediaStatus.QUEUED
    mf.manual_rank = 0
    mf.retry_count = 0
    mf.next_attempt_at = None
    mf.last_error = None
    mf.applied_rule_hash = None
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
    mf.status = MediaStatus.PENDING
    mf.retry_count = 0
    mf.next_attempt_at = None
    mf.last_error = None
    return schemas.ActionResult(ok=True, detail="fichier réinitialisé")
