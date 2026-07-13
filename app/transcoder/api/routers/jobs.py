"""Historique des jobs : liste paginée, détail, log complet, annulation, retry."""

from __future__ import annotations

import datetime
import os

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import PlainTextResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from transcoder.api import schemas
from transcoder.api.deps import get_queue_manager
from transcoder.core.queue import QueueManager
from transcoder.db import get_db
from transcoder.enums import JobState, MediaStatus
from transcoder.models import Job, MediaFile

router = APIRouter(prefix="/jobs", tags=["jobs"])


@router.get("", response_model=schemas.JobPage)
async def list_jobs(
    session: AsyncSession = Depends(get_db),
    library_id: int | None = None,
    state: JobState | None = None,
    codec: str | None = None,
    date_from: datetime.datetime | None = None,
    date_to: datetime.datetime | None = None,
    search: str | None = None,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> schemas.JobPage:
    stmt = select(Job)
    count_stmt = select(func.count()).select_from(Job)
    conditions = []
    if library_id is not None:
        conditions.append(Job.library_id == library_id)
    if state is not None:
        conditions.append(Job.state == state)
    if codec:
        conditions.append(Job.dst_codec == codec)
    if date_from is not None:
        conditions.append(Job.started_at >= date_from)
    if date_to is not None:
        conditions.append(Job.started_at <= date_to)
    if search:
        conditions.append(Job.src_path.ilike(f"%{search}%"))
    if conditions:
        stmt = stmt.where(*conditions)
        count_stmt = count_stmt.where(*conditions)
    total = (await session.scalar(count_stmt)) or 0
    rows = (
        (
            await session.execute(
                stmt.order_by(Job.id.desc()).limit(limit).offset(offset)
            )
        )
        .scalars()
        .all()
    )
    return schemas.JobPage(
        items=[schemas.JobOut.model_validate(r) for r in rows],
        meta=schemas.PageMeta(total=total, limit=limit, offset=offset),
    )


async def _get_job(session: AsyncSession, job_id: int) -> Job:
    job = await session.get(Job, job_id)
    if job is None:
        raise HTTPException(404, "job introuvable")
    return job


@router.get("/{job_id}", response_model=schemas.JobDetail)
async def job_detail(job_id: int, session: AsyncSession = Depends(get_db)) -> Job:
    return await _get_job(session, job_id)


@router.get("/{job_id}/log", response_class=PlainTextResponse)
async def job_log(
    job_id: int,
    download: bool = False,
    session: AsyncSession = Depends(get_db),
) -> PlainTextResponse:
    job = await _get_job(session, job_id)
    content = ""
    if job.log_path and os.path.exists(job.log_path):
        with open(job.log_path, encoding="utf-8", errors="replace") as fh:
            content = fh.read()
    elif job.log_tail:
        content = job.log_tail
    headers = {}
    if download:
        headers["Content-Disposition"] = f'attachment; filename="job_{job_id}.log"'
    return PlainTextResponse(content, headers=headers)


@router.post("/{job_id}/cancel", response_model=schemas.ActionResult)
async def cancel_job(
    job_id: int,
    session: AsyncSession = Depends(get_db),
    qm: QueueManager = Depends(get_queue_manager),
) -> schemas.ActionResult:
    job = await _get_job(session, job_id)
    if job.state in (JobState.RUNNING, JobState.VALIDATING, JobState.REPLACING):
        if qm.cancel_job(job_id):
            return schemas.ActionResult(ok=True, detail="SIGTERM envoyé à FFmpeg")
        raise HTTPException(409, "job actif mais introuvable dans ce process")
    if job.state is JobState.QUEUED:
        job.state = JobState.CANCELLED
        job.error_message = "annulé avant démarrage"
        mf = await session.get(MediaFile, job.media_file_id)
        if mf is not None and mf.status in (MediaStatus.QUEUED, MediaStatus.PROCESSING):
            mf.status = MediaStatus.PENDING
        return schemas.ActionResult(ok=True, detail="job retiré de la file")
    raise HTTPException(409, f"job non annulable (état {job.state.value})")


@router.post("/{job_id}/retry", response_model=schemas.ActionResult)
async def retry_job(job_id: int, session: AsyncSession = Depends(get_db)) -> schemas.ActionResult:
    job = await _get_job(session, job_id)
    if job.state not in (JobState.FAILED, JobState.CANCELLED, JobState.SKIPPED_NO_GAIN):
        raise HTTPException(409, f"job non rejouable (état {job.state.value})")
    mf = await session.get(MediaFile, job.media_file_id)
    if mf is None:
        raise HTTPException(404, "fichier associé introuvable")
    mf.status = MediaStatus.QUEUED
    mf.retry_count = 0
    mf.next_attempt_at = None
    mf.last_error = None
    return schemas.ActionResult(ok=True, detail="fichier remis en file d'attente")
