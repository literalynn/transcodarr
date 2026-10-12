"""Pipeline complet contre une vraie base PostgreSQL (§9.6, complément e2e).

Couvre ce que l'e2e logicielle ne peut pas : claim `FOR UPDATE SKIP LOCKED`,
préparation (sonde + règle depuis la DB), finalisation en base, et le
REMPLACEMENT ATOMIQUE réel (corbeille, mtime, tag) après les 9 validations.

L'encodeur matériel est remplacé par libx265 via monkeypatch du builder
(même squelette de commande) : aucun GPU requis. Skip propre si PostgreSQL
ou ffmpeg/libx265 sont absents.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

from transcoder.config import get_settings

def _find_tool(binary: str) -> str | None:
    """Cherche dans le PATH puis à l'emplacement jellyfin-ffmpeg (conteneur)."""
    found = shutil.which(binary)
    if found:
        return found
    candidate = f"/usr/lib/jellyfin-ffmpeg/{binary}"
    return candidate if os.access(candidate, os.X_OK) else None

FFMPEG = _find_tool("ffmpeg")
FFPROBE = _find_tool("ffprobe")


def _db_reachable() -> bool:
    import asyncpg

    async def ping() -> bool:
        settings = get_settings()
        try:
            conn = await asyncio.wait_for(
                asyncpg.connect(
                    host=settings.postgres_host,
                    port=settings.postgres_port,
                    user=settings.postgres_user,
                    password=settings.postgres_password,
                    database=settings.postgres_db,
                ),
                timeout=3.0,
            )
        except Exception:
            return False
        await conn.close()
        return True

    return asyncio.run(ping())


def _has_libx265() -> bool:
    if FFMPEG is None:
        return False
    out = subprocess.run(
        [FFMPEG, "-hide_banner", "-encoders"], capture_output=True, text=True
    ).stdout
    return " libx265 " in out


pytestmark = pytest.mark.skipif(
    FFMPEG is None or FFPROBE is None or not _has_libx265() or not _db_reachable(),
    reason="PostgreSQL ou ffmpeg/libx265 indisponibles : pipeline DB skippé",
)


async def test_claim_process_validate_replace(tmp_path: Path) -> None:
    from sqlalchemy import select

    from transcoder.core.capabilities import Capabilities, EncoderTest, GpuCapability
    from transcoder.core.cmd_builder import BuildParams, input_flags, stream_mapping_args
    from transcoder.core.dovi import DvCapabilities
    from transcoder.core.queue import QueueManager
    from transcoder.core.rules import build_transcoder_tag
    from transcoder.core.scanner import scan_library
    from transcoder.db import dispose_engine, session_scope
    from transcoder.enums import Codec, GpuApi, JobState, MediaStatus
    from transcoder.models import BitrateRule, Gpu, Job, Library, MediaFile
    from transcoder.runtime_config import RuntimeConfig
    from transcoder.seed_data import SEED_BITRATES, default_bufsize_kbps, default_max_kbps
    import transcoder.core.queue as queue_module

    assert FFMPEG is not None and FFPROBE is not None
    marker = uuid.uuid4().hex[:8]

    # --- Source : mire haut bitrate + audio, mtime ancien -------------------
    media_dir = tmp_path / "Films"
    media_dir.mkdir()
    src = media_dir / "film.mkv"
    subprocess.run(
        [
            FFMPEG, "-hide_banner", "-nostdin", "-y", "-v", "error",
            "-f", "lavfi", "-i", "testsrc2=duration=6:size=640x360:rate=25",
            "-f", "lavfi", "-i", "sine=duration=6",
            "-c:v", "libx264", "-b:v", "2500k", "-pix_fmt", "yuv420p",
            "-c:a", "aac", str(src),
        ],
        check=True,
    )
    os.utime(src, (1_600_000_000, 1_500_000_000))
    original_size = src.stat().st_size

    config = RuntimeConfig(
        ffmpeg_path=FFMPEG,
        ffprobe_path=FFPROBE,
        dovi_tool_path="/nonexistent",
        mkvpropedit_path="/nonexistent",
        tmp_strategy="sibling",
        tmp_dir=str(tmp_path / "tmp"),
        trash_dir=str(tmp_path / "trash"),
        dry_run_out_dir=str(tmp_path / "out"),
        log_dir=str(tmp_path / "logs"),
        trash_retention_days=14,
        log_retention_days=90,
        immediate_delete=False,
        dry_run_global=True,  # la bibliothèque surcharge à False
        max_concurrent_jobs=2,
        min_free_disk_gb=0.0,
        queue_paused=False,
        dv_ssim_threshold=0.90,
        dv_chroma_delta_max=8.0,
        validation_decode="full",
        max_retries=2,
        early_abort_no_gain=False,  # mire courte : la projection serait bruitée
        progress_snapshot_interval_s=1,
        preflight_gain_check=True,
        preflight_decode_check=True,
        auto_fallback=True,
        jellyfin_url=None,
        jellyfin_api_key=None,
    )

    # --- Bibliothèque + règles en base --------------------------------------
    async with session_scope() as session:
        lib = Library(
            name=f"pipeline-test-{marker}",
            path=str(media_dir),
            priority=10_000,
            scan_cron=None,
            dry_run=False,  # remplacement RÉEL testé (surcharge §9.4)
        )
        session.add(lib)
        gpu_row = Gpu(
            name=f"software-test-{marker}",
            drm_node=f"/dev/null-{marker}",
            api=GpuApi.QSV,
            worker_count=1,
            enabled=True,
        )
        session.add(gpu_row)
        await session.flush()
        lib_id = lib.id
        gpu_id = gpu_row.id
        for bucket, (sdr, _hdr) in SEED_BITRATES[Codec.HEVC]["Movies"].items():
            for is_hdr, target in ((False, 300), (True, sdr)):
                session.add(
                    BitrateRule(
                        library_id=lib_id,
                        codec=Codec.HEVC,
                        resolution_bucket=bucket,
                        is_hdr=is_hdr,
                        target_bitrate_kbps=target,
                        max_bitrate_kbps=default_max_kbps(target),
                        bufsize_kbps=default_bufsize_kbps(target),
                    )
                )
                session.add(
                    BitrateRule(
                        library_id=lib_id,
                        codec=Codec.AV1,
                        resolution_bucket=bucket,
                        is_hdr=is_hdr,
                        target_bitrate_kbps=target,
                        max_bitrate_kbps=default_max_kbps(target),
                        bufsize_kbps=default_bufsize_kbps(target),
                    )
                )

    try:
        # --- Scan : le fichier doit finir queued (pas de tag) ----------------
        async with session_scope() as session:
            lib_obj = await session.get(Library, lib_id)
            assert lib_obj is not None
            result = await scan_library(
                session,
                lib_obj,
                config=config,
                dv_caps=DvCapabilities(libplacebo_dv=False, libplacebo=False),
            )
            assert result.queued == 1, result.as_dict()
        async with session_scope() as session:
            mf = (
                await session.execute(
                    select(MediaFile).where(MediaFile.library_id == lib_id)
                )
            ).scalar_one()
            assert mf.status is MediaStatus.QUEUED
            assert mf.estimated_gain_bytes is not None and mf.estimated_gain_bytes > 0
            mf.manual_rank = 0  # tête de queue : le claim doit prendre CE fichier
            media_id = mf.id

        # --- Capacités factices + builder logiciel ---------------------------
        gpu_cap = GpuCapability(
            gpu_id=gpu_id,
            name="software-test",
            drm_node="/dev/null",
            configured_api=GpuApi.QSV,
            effective_api=GpuApi.QSV,
            fell_back_to_vaapi=False,
            encoders={"hevc_qsv": EncoderTest(encoder="hevc_qsv", ok=True)},
            p5_zero_copy=False,
        )
        caps = Capabilities(
            ffmpeg_ok=True,
            ffmpeg_version="test",
            ffprobe_ok=True,
            libplacebo=False,
            libplacebo_dv=False,
            dovi_tool_ok=False,
            mkvpropedit_ok=False,
            gpus=[gpu_cap],
        )

        def software_cmd(params: BuildParams) -> list[str]:
            # Même squelette §5.3 que le builder (plan de pistes, options
            # d'entrée de la stratégie), encodeur logiciel
            return [
                params.ffmpeg_path, "-hide_banner", "-nostdin", "-y",
                *input_flags(params.strategy, params.input_format),
                "-i", params.src_path,
                *stream_mapping_args(params.stream_plan, "libx265"),
                "-preset:v", "ultrafast",
                "-b:v", f"{params.rule.target_kbps}k",
                "-maxrate", f"{params.rule.max_kbps}k",
                "-bufsize", f"{params.rule.bufsize_kbps}k",
                "-pix_fmt", "yuv420p",
                "-map_metadata", "0", "-map_chapters", "0",
                "-metadata", f"TRANSCODER_TAG={build_transcoder_tag(params.rule)}",
                "-progress", "pipe:1", "-nostats", "-loglevel", "warning",
                params.dst_path,
            ]

        original_builder = queue_module.build_ffmpeg_cmd
        queue_module.build_ffmpeg_cmd = software_cmd  # type: ignore[assignment]
        try:
            qm = QueueManager(caps)
            work = await qm._claim([Codec.HEVC])
            assert work is not None and work.media_id == media_id
            await qm._process(work, gpu_cap, 0, config)
        finally:
            queue_module.build_ffmpeg_cmd = original_builder  # type: ignore[assignment]

        # --- Assertions : DB + filesystem ------------------------------------
        async with session_scope() as session:
            job = (
                await session.execute(
                    select(Job).where(Job.media_file_id == media_id).order_by(Job.id.desc())
                )
            ).scalars().first()
            assert job is not None
            assert job.state is JobState.SUCCESS, (job.state, job.error_message)
            assert job.dry_run is False
            assert job.exit_code == 0
            assert job.validation_report is not None
            assert job.validation_report["outcome"] == "passed"
            assert job.bytes_saved is not None and job.bytes_saved > 0
            assert job.trashed_path is not None
            assert job.dst_path == str(src)

            mf2 = await session.get(MediaFile, media_id)
            assert mf2 is not None
            assert mf2.status is MediaStatus.DONE
            assert mf2.video_codec == "hevc"
            assert mf2.applied_rule_hash == job.rule_hash
            assert mf2.transcoder_tag is not None and "jftranscoder-v1" in mf2.transcoder_tag

            # Remplacement atomique réel (§5.6)
            assert src.exists()
            assert src.stat().st_size < original_size  # réencodé, plus petit
            assert os.stat(src).st_mtime == pytest.approx(1_500_000_000)  # mtime restauré
            assert Path(job.trashed_path).exists()  # original en corbeille
            assert Path(job.trashed_path).stat().st_size == original_size
            assert job.log_path is not None and os.path.exists(job.log_path)

        # --- Anti-boucle : un rescan ne re-queue PAS le fichier (§5.7) -------
        async with session_scope() as session:
            lib_obj = await session.get(Library, lib_id)
            assert lib_obj is not None
            rescan = await scan_library(
                session,
                lib_obj,
                config=config,
                dv_caps=DvCapabilities(libplacebo_dv=False, libplacebo=False),
            )
            assert rescan.queued == 0, rescan.as_dict()
        async with session_scope() as session:
            mf3 = await session.get(MediaFile, media_id)
            assert mf3 is not None and mf3.status is MediaStatus.DONE
    finally:
        async with session_scope() as session:
            lib_obj = await session.get(Library, lib_id)
            if lib_obj is not None:
                await session.delete(lib_obj)
            gpu_obj = await session.get(Gpu, gpu_id)
            if gpu_obj is not None:
                await session.delete(gpu_obj)
        await dispose_engine()
