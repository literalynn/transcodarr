"""Queue : état, pause/reprise (globale ou par bibliothèque), réordonnancement."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from transcoder.api import schemas
from transcoder.api.deps import get_queue_manager
from transcoder.core.queue import QueueManager
from transcoder.db import get_db
from transcoder.enums import MediaStatus
from transcoder.models import AppSetting, Library, MediaFile
from transcoder.runtime_config import load_runtime_config

router = APIRouter(prefix="/queue", tags=["queue"])


@router.get("", response_model=schemas.QueueState)
async def queue_state(
    session: AsyncSession = Depends(get_db),
    qm: QueueManager = Depends(get_queue_manager),
) -> schemas.QueueState:
    config = await load_runtime_config(session)
    waiting = (
        await session.scalar(
            select(func.count())
            .select_from(MediaFile)
            .where(MediaFile.status == MediaStatus.QUEUED)
        )
    ) or 0
    return schemas.QueueState(
        paused=config.queue_paused,
        workers=qm.worker_count(),
        active=[schemas.ActiveJob(**j) for j in qm.active_jobs()],
        waiting_count=waiting,
    )


async def _set_paused(
    session: AsyncSession, paused: bool, library_id: int | None
) -> schemas.ActionResult:
    if library_id is not None:
        lib = await session.get(Library, library_id)
        if lib is None:
            raise HTTPException(404, "bibliothèque introuvable")
        lib.paused = paused
        return schemas.ActionResult(
            ok=True, detail=f"bibliothèque {lib.name} {'en pause' if paused else 'reprise'}"
        )
    await session.execute(
        update(AppSetting).where(AppSetting.key == "queue_paused").values(value=paused)
    )
    return schemas.ActionResult(
        ok=True, detail="queue globale en pause" if paused else "queue globale reprise"
    )


@router.post("/pause", response_model=schemas.ActionResult)
async def pause(
    library_id: int | None = None, session: AsyncSession = Depends(get_db)
) -> schemas.ActionResult:
    return await _set_paused(session, True, library_id)


@router.post("/resume", response_model=schemas.ActionResult)
async def resume(
    library_id: int | None = None, session: AsyncSession = Depends(get_db)
) -> schemas.ActionResult:
    return await _set_paused(session, False, library_id)


@router.post("/reorder", response_model=schemas.ActionResult)
async def reorder(
    payload: schemas.ReorderRequest, session: AsyncSession = Depends(get_db)
) -> schemas.ActionResult:
    """Priorité manuelle (drag §7.2) : l'ordre reçu devient manual_rank 1..N."""
    for rank, media_id in enumerate(payload.media_file_ids, start=1):
        await session.execute(
            update(MediaFile).where(MediaFile.id == media_id).values(manual_rank=rank)
        )
    return schemas.ActionResult(ok=True, detail=f"{len(payload.media_file_ids)} fichier(s) réordonné(s)")
