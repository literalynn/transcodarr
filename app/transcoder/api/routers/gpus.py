"""GPU : CRUD, auto-détection des nodes DRM, test de mire 5 s (§7.6)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from transcoder.api import schemas
from transcoder.api.deps import get_capabilities, get_queue_manager
from transcoder.core.capabilities import (
    Capabilities,
    GpuConfig,
    detect_drm_nodes,
    probe_gpu,
    run_encoder_test,
)
from transcoder.core.queue import QueueManager
from transcoder.db import get_db
from transcoder.enums import Codec
from transcoder.models import Gpu
from transcoder.runtime_config import load_runtime_config

router = APIRouter(prefix="/gpus", tags=["gpus"])


@router.get("/detect", response_model=schemas.DetectedNodes)
async def detect_nodes() -> schemas.DetectedNodes:
    return schemas.DetectedNodes(nodes=detect_drm_nodes())


@router.get("", response_model=list[schemas.GpuOut])
async def list_gpus(session: AsyncSession = Depends(get_db)) -> list[Gpu]:
    return list((await session.execute(select(Gpu).order_by(Gpu.id))).scalars().all())


@router.post("", response_model=schemas.GpuOut, status_code=201)
async def create_gpu(
    payload: schemas.GpuCreate,
    session: AsyncSession = Depends(get_db),
    caps: Capabilities = Depends(get_capabilities),
    qm: QueueManager = Depends(get_queue_manager),
) -> Gpu:
    dup = await session.scalar(
        select(func.count()).select_from(Gpu).where(Gpu.drm_node == payload.drm_node)
    )
    if dup:
        raise HTTPException(409, "node DRM déjà déclaré")
    gpu = Gpu(
        name=payload.name,
        drm_node=payload.drm_node,
        api=payload.api,
        worker_count=payload.worker_count,
        enabled=payload.enabled,
    )
    session.add(gpu)
    await session.flush()
    config = await load_runtime_config(session)
    caps.gpus.append(
        await probe_gpu(
            config.ffmpeg_path,
            GpuConfig(id=gpu.id, name=gpu.name, drm_node=gpu.drm_node, api=gpu.api),
            libplacebo=caps.libplacebo,
        )
    )
    await qm.reload_workers()
    return gpu


@router.patch("/{gpu_id}", response_model=schemas.GpuOut)
async def patch_gpu(
    gpu_id: int,
    payload: schemas.GpuPatch,
    session: AsyncSession = Depends(get_db),
    caps: Capabilities = Depends(get_capabilities),
    qm: QueueManager = Depends(get_queue_manager),
) -> Gpu:
    gpu = await session.get(Gpu, gpu_id)
    if gpu is None:
        raise HTTPException(404, "GPU introuvable")
    data = payload.model_dump(exclude_unset=True)
    for field_name in ("name", "drm_node", "api", "worker_count", "enabled"):
        if field_name in data and data[field_name] is not None:
            setattr(gpu, field_name, data[field_name])
    await session.flush()
    if "drm_node" in data or "api" in data:
        config = await load_runtime_config(session)
        cap = await probe_gpu(
            config.ffmpeg_path,
            GpuConfig(id=gpu.id, name=gpu.name, drm_node=gpu.drm_node, api=gpu.api),
            libplacebo=caps.libplacebo,
        )
        caps.gpus = [g for g in caps.gpus if g.gpu_id != gpu.id] + [cap]
    await qm.reload_workers()
    return gpu


@router.delete("/{gpu_id}", status_code=204)
async def delete_gpu(
    gpu_id: int,
    session: AsyncSession = Depends(get_db),
    caps: Capabilities = Depends(get_capabilities),
    qm: QueueManager = Depends(get_queue_manager),
) -> None:
    gpu = await session.get(Gpu, gpu_id)
    if gpu is None:
        raise HTTPException(404, "GPU introuvable")
    await session.delete(gpu)
    await session.flush()
    caps.gpus = [g for g in caps.gpus if g.gpu_id != gpu_id]
    await qm.reload_workers()


@router.post("/{gpu_id}/test", response_model=schemas.GpuTestResult)
async def test_gpu(
    gpu_id: int,
    session: AsyncSession = Depends(get_db),
    caps: Capabilities = Depends(get_capabilities),
) -> schemas.GpuTestResult:
    """Encode 5 s de mire en HEVC ET AV1 sur le node — bouton « Tester » (§9.5.4)."""
    gpu = await session.get(Gpu, gpu_id)
    if gpu is None:
        raise HTTPException(404, "GPU introuvable")
    config = await load_runtime_config(session)
    results: dict[str, dict[str, object]] = {}
    for codec in (Codec.HEVC, Codec.AV1):
        test = await run_encoder_test(
            config.ffmpeg_path, codec=codec, api=gpu.api, drm_node=gpu.drm_node
        )
        results[test.encoder] = {"ok": test.ok, "error": test.error}
    # Rafraîchit aussi les capacités persistées en mémoire
    cap = await probe_gpu(
        config.ffmpeg_path,
        GpuConfig(id=gpu.id, name=gpu.name, drm_node=gpu.drm_node, api=gpu.api),
        libplacebo=caps.libplacebo,
    )
    caps.gpus = [g for g in caps.gpus if g.gpu_id != gpu.id] + [cap]
    return schemas.GpuTestResult(gpu_id=gpu_id, results=results)
