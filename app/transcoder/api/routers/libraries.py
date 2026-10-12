"""Bibliothèques : CRUD, règles de bitrate, scan, préversion changement de codec."""

from __future__ import annotations

import asyncio
import decimal
import os

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from transcoder.api import schemas
from transcoder.api.deps import get_capabilities, get_scan_scheduler
from transcoder.core.capabilities import Capabilities
from transcoder.db import get_db
from transcoder.enums import Codec, MediaStatus
from transcoder.models import BitrateRule, Library, MediaFile
from transcoder.scan_service import ScanScheduler, run_scan
from transcoder.seed_data import SEED_BITRATES, default_bufsize_kbps, default_max_kbps

router = APIRouter(prefix="/libraries", tags=["libraries"])


async def _get_library(session: AsyncSession, library_id: int) -> Library:
    lib = await session.get(Library, library_id)
    if lib is None:
        raise HTTPException(404, "bibliothèque introuvable")
    return lib


@router.get("", response_model=list[schemas.LibraryOut])
async def list_libraries(session: AsyncSession = Depends(get_db)) -> list[Library]:
    rows = (await session.execute(select(Library).order_by(Library.id))).scalars().all()
    return list(rows)


@router.post("", response_model=schemas.LibraryOut, status_code=201)
async def create_library(
    payload: schemas.LibraryCreate, session: AsyncSession = Depends(get_db)
) -> Library:
    # Validation d'existence du chemin côté serveur (§7.5)
    if not os.path.isdir(payload.path):
        raise HTTPException(422, f"chemin inexistant dans le conteneur : {payload.path}")
    dup = await session.scalar(
        select(func.count())
        .select_from(Library)
        .where((Library.name == payload.name) | (Library.path == payload.path))
    )
    if dup:
        raise HTTPException(409, "nom ou chemin déjà utilisé")
    lib = Library(
        name=payload.name,
        path=payload.path.rstrip("/"),
        enabled=payload.enabled,
        output_codec=payload.output_codec,
        container=payload.container,
        hdr_policy=payload.hdr_policy,
        dv_policy=payload.dv_policy,
        date_filter_after=payload.date_filter_after,
        priority=payload.priority,
        include_extensions=payload.include_extensions,
        exclude_globs=payload.exclude_globs,
        min_gain_percent=decimal.Decimal(f"{payload.min_gain_percent:.2f}"),
        scan_cron=payload.scan_cron,
        dry_run=payload.dry_run,
    )
    session.add(lib)
    await session.flush()
    # Jeu complet de règles par défaut (échelle « Movies ») : la
    # bibliothèque est fonctionnelle immédiatement, l'UI permet d'éditer.
    for codec in (Codec.HEVC, Codec.AV1):
        for bucket, (sdr, hdr) in SEED_BITRATES[codec]["Movies"].items():
            for is_hdr, target in ((False, sdr), (True, hdr)):
                session.add(
                    BitrateRule(
                        library_id=lib.id,
                        codec=codec,
                        resolution_bucket=bucket,
                        is_hdr=is_hdr,
                        target_bitrate_kbps=target,
                        max_bitrate_kbps=default_max_kbps(target),
                        bufsize_kbps=default_bufsize_kbps(target),
                    )
                )
    await session.flush()
    return lib


@router.patch("/{library_id}", response_model=schemas.LibraryOut)
async def patch_library(
    library_id: int,
    payload: schemas.LibraryPatch,
    session: AsyncSession = Depends(get_db),
    scheduler: ScanScheduler = Depends(get_scan_scheduler),
) -> Library:
    lib = await _get_library(session, library_id)
    data = payload.model_dump(exclude_unset=True)
    if "path" in data and data["path"] is not None and not os.path.isdir(data["path"]):
        raise HTTPException(422, f"chemin inexistant dans le conteneur : {data['path']}")

    simple_fields = (
        "name", "path", "enabled", "paused", "output_codec", "container",
        "hdr_policy", "dv_policy", "date_filter_after", "priority",
        "include_extensions", "exclude_globs", "scan_cron", "dry_run",
    )
    for field_name in simple_fields:
        if field_name in data and data[field_name] is not None:
            setattr(lib, field_name, data[field_name])
    if "min_gain_percent" in data and data["min_gain_percent"] is not None:
        lib.min_gain_percent = decimal.Decimal(f"{data['min_gain_percent']:.2f}")
    if payload.clear_date_filter:
        lib.date_filter_after = None
    if payload.clear_scan_cron:
        lib.scan_cron = None
    if payload.clear_dry_run:
        lib.dry_run = None
    await session.flush()
    await scheduler.reload()
    return lib


@router.delete("/{library_id}", status_code=204)
async def delete_library(
    library_id: int,
    session: AsyncSession = Depends(get_db),
    scheduler: ScanScheduler = Depends(get_scan_scheduler),
) -> None:
    lib = await _get_library(session, library_id)
    await session.delete(lib)
    await session.flush()
    await scheduler.reload()


# ---------------------------------------------------------------------------
# Règles de bitrate — PUT transactionnel du jeu complet
# ---------------------------------------------------------------------------
@router.get("/{library_id}/bitrate-rules", response_model=list[schemas.BitrateRuleOut])
async def get_bitrate_rules(
    library_id: int, session: AsyncSession = Depends(get_db)
) -> list[BitrateRule]:
    await _get_library(session, library_id)
    rows = (
        (
            await session.execute(
                select(BitrateRule)
                .where(BitrateRule.library_id == library_id)
                .order_by(BitrateRule.codec, BitrateRule.resolution_bucket, BitrateRule.is_hdr)
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


@router.put("/{library_id}/bitrate-rules", response_model=list[schemas.BitrateRuleOut])
async def put_bitrate_rules(
    library_id: int,
    payload: list[schemas.BitrateRuleIn],
    session: AsyncSession = Depends(get_db),
) -> list[BitrateRule]:
    """Remplace TOUT le jeu de règles en une transaction (§ plan module 5)."""
    await _get_library(session, library_id)
    seen: set[tuple[Codec, str, bool]] = set()
    for rule in payload:
        key = (rule.codec, rule.resolution_bucket.value, rule.is_hdr)
        if key in seen:
            raise HTTPException(422, f"règle en double : {key}")
        seen.add(key)
        if rule.max_bitrate_kbps < rule.target_bitrate_kbps:
            raise HTTPException(422, "max_bitrate_kbps < target_bitrate_kbps")
        if rule.bufsize_kbps < rule.target_bitrate_kbps:
            raise HTTPException(422, "bufsize_kbps < target_bitrate_kbps")

    existing = (
        (await session.execute(select(BitrateRule).where(BitrateRule.library_id == library_id)))
        .scalars()
        .all()
    )
    for row in existing:
        await session.delete(row)
    await session.flush()
    created: list[BitrateRule] = []
    for rule in payload:
        row = BitrateRule(
            library_id=library_id,
            codec=rule.codec,
            resolution_bucket=rule.resolution_bucket,
            is_hdr=rule.is_hdr,
            target_bitrate_kbps=rule.target_bitrate_kbps,
            max_bitrate_kbps=rule.max_bitrate_kbps,
            bufsize_kbps=rule.bufsize_kbps,
        )
        session.add(row)
        created.append(row)
    await session.flush()
    return created


# ---------------------------------------------------------------------------
# Scan et préversion de changement de codec
# ---------------------------------------------------------------------------
@router.post("/{library_id}/scan", response_model=schemas.ScanTriggered)
async def trigger_scan(
    library_id: int,
    session: AsyncSession = Depends(get_db),
    caps: Capabilities = Depends(get_capabilities),
) -> schemas.ScanTriggered:
    await _get_library(session, library_id)
    task = asyncio.create_task(run_scan(caps.dv_caps, [library_id]))
    # le résultat arrive par WebSocket (scan_finished) ; la tâche vit seule
    task.add_done_callback(lambda t: t.exception())
    return schemas.ScanTriggered(started=True, library_id=library_id)


@router.get("/{library_id}/codec-change-preview", response_model=schemas.CodecChangePreview)
async def codec_change_preview(
    library_id: int, codec: Codec, session: AsyncSession = Depends(get_db)
) -> schemas.CodecChangePreview:
    """« N fichiers déjà traités seront remis en file d'attente » (§7.5)."""
    lib = await _get_library(session, library_id)
    count = 0
    if codec != lib.output_codec:
        count = (
            await session.scalar(
                select(func.count())
                .select_from(MediaFile)
                .where(
                    MediaFile.library_id == library_id,
                    MediaFile.status.in_(
                        (MediaStatus.DONE, MediaStatus.SKIPPED_NO_GAIN)
                    ),
                )
            )
        ) or 0
    return schemas.CodecChangePreview(
        library_id=library_id,
        current_codec=lib.output_codec,
        target_codec=codec,
        requeued_count=count,
    )
