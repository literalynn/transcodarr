"""Échecs de production rejoués de bout en bout contre PostgreSQL.

- MP4 avec sous-titres mov_text + piste data (codes 218/234 en prod) :
  réussite dès la première tentative grâce au plan de pistes ;
- repli automatique : une première tentative échoue au mux (commande
  « ancienne manière » -map 0 -c copy), la catégorie est reconnue, la
  stratégie safe_streams est retentée immédiatement et réussit ;
- échec définitif : plus aucun repli possible → failed, un rescan ne le
  remet PAS en file (boucle de 161 tentatives), la relance manuelle oui ;
- statistiques de fiabilité cohérentes avec ces issues.

Encodeur logiciel (libx265) à la place du GPU, même squelette de commande.
"""

from __future__ import annotations

import os
import subprocess
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.test_pipeline_db import FFMPEG, FFPROBE, _db_reachable, _has_libx265

pytestmark = pytest.mark.skipif(
    FFMPEG is None or FFPROBE is None or not _has_libx265() or not _db_reachable(),
    reason="PostgreSQL ou ffmpeg/libx265 indisponibles : tests de reprise DB skippés",
)


@dataclass
class Env:
    lib_id: int
    gpu_id: int
    media_dir: Path
    tmp_path: Path


def _make_mp4_with_mov_text_and_data(path: Path) -> None:
    assert FFMPEG is not None
    srt = path.with_suffix(".srt")
    srt.write_text("1\n00:00:00,500 --> 00:00:02,000\nBonjour\n", encoding="utf-8")
    subprocess.run(
        [
            FFMPEG, "-hide_banner", "-nostdin", "-y", "-v", "error",
            "-f", "lavfi", "-i", "testsrc2=duration=6:size=640x360:rate=25",
            "-f", "lavfi", "-i", "sine=duration=6",
            "-i", str(srt),
            "-map", "0", "-map", "1", "-map", "2",
            "-c:v", "libx264", "-b:v", "2500k", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-c:s", "mov_text",
            "-timecode", "00:00:00:00",  # piste data tmcd
            str(path),
        ],
        check=True,
    )
    srt.unlink()
    os.utime(path, (1_600_000_000, 1_500_000_000))


def _config(tmp_path: Path) -> object:
    from transcoder.runtime_config import RuntimeConfig

    assert FFMPEG is not None and FFPROBE is not None
    return RuntimeConfig(
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
        dry_run_global=True,
        max_concurrent_jobs=2,
        min_free_disk_gb=0.0,
        queue_paused=False,
        dv_ssim_threshold=0.90,
        dv_chroma_delta_max=8.0,
        validation_decode="full",
        max_retries=2,
        early_abort_no_gain=False,
        progress_snapshot_interval_s=1,
        preflight_gain_check=True,
        preflight_decode_check=True,
        auto_fallback=True,
        jellyfin_url=None,
        jellyfin_api_key=None,
    )


@pytest.fixture
async def env(tmp_path: Path) -> AsyncIterator[Env]:
    from transcoder.db import dispose_engine, session_scope
    from transcoder.enums import Codec, GpuApi
    from transcoder.models import BitrateRule, Gpu, Library
    from transcoder.seed_data import SEED_BITRATES, default_bufsize_kbps, default_max_kbps

    marker = uuid.uuid4().hex[:8]
    media_dir = tmp_path / "Animes"
    media_dir.mkdir()
    async with session_scope() as session:
        lib = Library(
            name=f"recovery-{marker}", path=str(media_dir), priority=10_000,
            scan_cron=None, dry_run=False,
        )
        gpu = Gpu(
            name=f"soft-{marker}", drm_node=f"/dev/null-{marker}", api=GpuApi.QSV,
            worker_count=1, enabled=True,
        )
        session.add_all([lib, gpu])
        await session.flush()
        for bucket in SEED_BITRATES[Codec.HEVC]["Films"]:
            for is_hdr in (False, True):
                session.add(
                    BitrateRule(
                        library_id=lib.id, codec=Codec.HEVC, resolution_bucket=bucket,
                        is_hdr=is_hdr, target_bitrate_kbps=300,
                        max_bitrate_kbps=default_max_kbps(300),
                        bufsize_kbps=default_bufsize_kbps(300),
                    )
                )
        ids = (lib.id, gpu.id)
    try:
        yield Env(lib_id=ids[0], gpu_id=ids[1], media_dir=media_dir, tmp_path=tmp_path)
    finally:
        async with session_scope() as session:
            lib_obj = await session.get(Library, ids[0])
            if lib_obj is not None:
                await session.delete(lib_obj)
            gpu_obj = await session.get(Gpu, ids[1])
            if gpu_obj is not None:
                await session.delete(gpu_obj)
        await dispose_engine()


def _caps(gpu_id: int) -> tuple[object, object]:
    from transcoder.core.capabilities import Capabilities, EncoderTest, GpuCapability
    from transcoder.enums import GpuApi

    gpu_cap = GpuCapability(
        gpu_id=gpu_id, name="software-test", drm_node="/dev/null",
        configured_api=GpuApi.QSV, effective_api=GpuApi.QSV, fell_back_to_vaapi=False,
        encoders={"hevc_qsv": EncoderTest(encoder="hevc_qsv", ok=True)},
    )
    caps = Capabilities(
        ffmpeg_ok=True, ffmpeg_version="test", ffprobe_ok=True, libplacebo=False,
        libplacebo_dv=False, dovi_tool_ok=False, mkvpropedit_ok=False, gpus=[gpu_cap],
    )
    return gpu_cap, caps


def _software_builder(legacy_for: Callable[[object], bool]) -> Callable[[object], list[str]]:
    """Builder logiciel ; ``legacy_for(strategy)`` → commande « à l'ancienne »
    (-map 0 -c copy) qui reproduit les échecs de mux de la production."""
    from transcoder.core.cmd_builder import BuildParams, input_flags, stream_mapping_args
    from transcoder.core.rules import build_transcoder_tag

    def build(params: BuildParams) -> list[str]:  # type: ignore[misc]
        if legacy_for(params.strategy):
            mapping = ["-map", "0", "-map", "-0:v", "-map", "0:v:0", "-c", "copy", "-c:v", "libx265"]
        else:
            mapping = stream_mapping_args(params.stream_plan, "libx265")
        return [
            params.ffmpeg_path, "-hide_banner", "-nostdin", "-y",
            *input_flags(params.strategy, params.input_format),
            "-i", params.src_path, *mapping,
            "-preset:v", "ultrafast", "-b:v", f"{params.rule.target_kbps}k",
            "-pix_fmt", "yuv420p", "-map_metadata", "0", "-map_chapters", "0",
            "-metadata", f"TRANSCODER_TAG={build_transcoder_tag(params.rule)}",
            "-progress", "pipe:1", "-nostats", "-loglevel", "warning",
            params.dst_path,
        ]

    return build  # type: ignore[return-value]


async def _scan(env: Env) -> dict[str, object]:
    from transcoder.core.dovi import DvCapabilities
    from transcoder.core.scanner import scan_library
    from transcoder.db import session_scope
    from transcoder.models import Library

    async with session_scope() as session:
        lib = await session.get(Library, env.lib_id)
        assert lib is not None
        result = await scan_library(
            session, lib, config=_config(env.tmp_path),  # type: ignore[arg-type]
            dv_caps=DvCapabilities(libplacebo_dv=False, libplacebo=False),
        )
        return result.as_dict()


async def _run_once(env: Env, builder: Callable[[object], list[str]]) -> None:
    import transcoder.core.queue as queue_module
    from transcoder.core.queue import QueueManager
    from transcoder.enums import Codec

    gpu_cap, caps = _caps(env.gpu_id)
    original = queue_module.build_ffmpeg_cmd
    queue_module.build_ffmpeg_cmd = builder  # type: ignore[assignment]
    try:
        qm = QueueManager(caps)  # type: ignore[arg-type]
        work = await qm._claim([Codec.HEVC])
        assert work is not None
        await qm._process(work, gpu_cap, 0, _config(env.tmp_path))  # type: ignore[arg-type]
    finally:
        queue_module.build_ffmpeg_cmd = original  # type: ignore[assignment]


async def _jobs_and_file(env: Env) -> tuple[list[object], object]:
    from sqlalchemy import select

    from transcoder.db import session_scope
    from transcoder.models import Job, MediaFile

    async with session_scope() as session:
        mf = (
            await session.execute(select(MediaFile).where(MediaFile.library_id == env.lib_id))
        ).scalar_one()
        jobs = (
            await session.execute(select(Job).where(Job.media_file_id == mf.id).order_by(Job.id))
        ).scalars().all()
        return list(jobs), mf


async def test_mov_text_and_data_stream_succeed_first_try(env: Env) -> None:
    from transcoder.enums import JobState, MediaStatus

    src = env.media_dir / "episode.mp4"
    _make_mp4_with_mov_text_and_data(src)
    assert (await _scan(env))["queued"] == 1

    await _run_once(env, _software_builder(lambda _s: False))

    jobs, mf = await _jobs_and_file(env)
    assert len(jobs) == 1
    job = jobs[0]
    assert job.state is JobState.SUCCESS, (job.error_message, job.failure_detail)  # type: ignore[attr-defined]
    assert job.attempt == 1 and job.strategy == "standard"  # type: ignore[attr-defined]
    plan = job.diagnostics["stream_plan"]["streams"]  # type: ignore[attr-defined]
    actions = {s["kind"]: (s["action"], s["target_codec"]) for s in plan}
    assert actions["subtitle"] == ("convert", "srt")
    assert actions["data"][0] == "drop"
    assert mf.status is MediaStatus.DONE  # type: ignore[attr-defined]
    # MP4 → MKV : extension changée, sous-titres présents en SubRip
    out = subprocess.run(
        [FFPROBE or "ffprobe", "-v", "error", "-show_entries", "stream=codec_type,codec_name",
         "-of", "csv=p=0", mf.path],  # type: ignore[attr-defined]
        capture_output=True, text=True, check=True,
    ).stdout
    assert "subrip,subtitle" in out and "data" not in out


async def test_mux_failure_recovers_through_fallback(env: Env) -> None:
    from transcoder.api.routers.stats import reliability, savings_over_time, throughput
    from transcoder.db import session_scope
    from transcoder.enums import JobState, MediaStatus

    _make_mp4_with_mov_text_and_data(env.media_dir / "episode.mp4")
    await _scan(env)
    # Tentative 1 : commande « à l'ancienne » → échec de mux comme en prod
    builder = _software_builder(lambda strategy: strategy.value == "standard")  # type: ignore[attr-defined]
    await _run_once(env, builder)
    jobs, mf = await _jobs_and_file(env)
    first = jobs[0]
    assert first.state is JobState.FAILED  # type: ignore[attr-defined]
    assert first.failure_category in ("mux_subtitle", "mux_data")  # type: ignore[attr-defined]
    assert first.failure_detail and "libva" not in first.failure_detail  # type: ignore[attr-defined]
    assert first.diagnostics["decision"]["kind"] == "fallback"  # type: ignore[attr-defined]
    assert mf.status is MediaStatus.QUEUED  # type: ignore[attr-defined]
    # Erreur de sous-titres → réencodage des sous-titres avant toute exclusion
    assert mf.next_strategy == "reencode_subs"  # type: ignore[attr-defined]
    assert mf.next_attempt_at is None  # repli immédiat, pas de backoff  # type: ignore[attr-defined]

    # Tentative 2 : stratégie de repli → succès
    await _run_once(env, builder)
    jobs, mf = await _jobs_and_file(env)
    second = jobs[1]
    assert second.state is JobState.SUCCESS, (second.error_message, second.failure_detail)  # type: ignore[attr-defined]
    assert second.attempt == 2 and second.strategy == "reencode_subs"  # type: ignore[attr-defined]
    assert mf.status is MediaStatus.DONE and mf.attempt_count == 0  # type: ignore[attr-defined]
    assert mf.strategies_tried == []  # type: ignore[attr-defined]

    async with session_scope() as session:
        stats = await reliability(session=session, days=None)
        last_day = await reliability(session=session, days=1)
        savings = await savings_over_time(session=session, days=1)
        rate = await throughput(session=session, days=7)
    assert stats["recovered_by_fallback"] >= 1
    assert stats["counts"]["failed"] >= 1
    assert any(s["strategy"] == "reencode_subs" and s["success"] >= 1 for s in stats["strategies"])
    # Périodes : créneaux horaires sur 24 h, tous présents, totaux concordants
    assert last_day["unit"] == "hour" and len(last_day["timeline"]) == 24
    assert sum(p["success"] for p in last_day["timeline"]) == last_day["counts"]["success"]
    assert savings["unit"] == "hour" and len(savings["points"]) == 24
    assert savings["points"][-1]["cumulative"] == savings["total_bytes"]
    assert rate["unit"] == "day" and len(rate["points"]) == 7
    assert rate["files"] == sum(p["files"] for p in rate["points"]) >= 1


async def test_terminal_failure_is_not_requeued_by_scan(env: Env) -> None:
    from transcoder.core.queue import retry_failed_files
    from transcoder.db import session_scope
    from transcoder.enums import JobState, MediaStatus

    _make_mp4_with_mov_text_and_data(env.media_dir / "episode.mp4")
    await _scan(env)
    always_legacy = _software_builder(lambda _s: True)
    await _run_once(env, always_legacy)  # standard : échec → repli
    # reencode_subs : échec → plus rien d'utile (safe_streams produirait le
    # MÊME mapping pour ce fichier : repli sauté au lieu d'un encodage pour rien)
    await _run_once(env, always_legacy)
    jobs, mf = await _jobs_and_file(env)
    assert [j.state for j in jobs] == [JobState.FAILED] * 2  # type: ignore[attr-defined]
    assert [j.strategy for j in jobs] == ["standard", "reencode_subs"]  # type: ignore[attr-defined]
    assert jobs[-1].diagnostics["decision"]["kind"] == "final"  # type: ignore[attr-defined]
    assert mf.status is MediaStatus.FAILED  # type: ignore[attr-defined]
    assert mf.applied_rule_hash is not None  # type: ignore[attr-defined]

    # L'ancien bug : chaque scan remettait ce fichier en file
    for _ in range(2):
        result = await _scan(env)
        assert result["queued"] == 0, result
    _, mf = await _jobs_and_file(env)
    assert mf.status is MediaStatus.FAILED  # type: ignore[attr-defined]

    # Relance manuelle : nouvelle série complète, depuis la stratégie standard
    async with session_scope() as session:
        assert await retry_failed_files(session, library_id=env.lib_id) == 1
    _, mf = await _jobs_and_file(env)
    assert mf.status is MediaStatus.QUEUED  # type: ignore[attr-defined]
    assert mf.attempt_count == 0 and mf.strategies_tried == []  # type: ignore[attr-defined]
    await _run_once(env, _software_builder(lambda _s: False))
    jobs, mf = await _jobs_and_file(env)
    assert jobs[-1].state is JobState.SUCCESS and jobs[-1].attempt == 1  # type: ignore[attr-defined]


async def test_empty_file_is_reported_as_such(env: Env) -> None:
    from transcoder.core.failures import FailureCategory
    from transcoder.core.scanner import EMPTY_FILE_ERROR
    from transcoder.enums import MediaStatus

    (env.media_dir / "episode-vide.mkv").write_bytes(b"")
    await _scan(env)
    jobs, mf = await _jobs_and_file(env)
    assert jobs == []
    assert mf.status is MediaStatus.FAILED  # type: ignore[attr-defined]
    assert mf.last_error == EMPTY_FILE_ERROR  # type: ignore[attr-defined]
    assert mf.failure_category == FailureCategory.SOURCE_UNREADABLE.value  # type: ignore[attr-defined]


async def test_hopeless_gain_is_skipped_without_encoding(env: Env) -> None:
    """Source déjà sous la cible : pas de job d'encodage, statut sans gain."""
    from sqlalchemy import update

    from transcoder.db import session_scope
    from transcoder.enums import MediaStatus
    from transcoder.models import BitrateRule

    _make_mp4_with_mov_text_and_data(env.media_dir / "episode.mp4")
    async with session_scope() as session:
        await session.execute(
            update(BitrateRule)
            .where(BitrateRule.library_id == env.lib_id)
            .values(target_bitrate_kbps=20000, max_bitrate_kbps=30000, bufsize_kbps=40000)
        )
    result = await _scan(env)
    assert result["queued"] == 0 and result["skipped_no_gain_predicted"] == 1, result
    jobs, mf = await _jobs_and_file(env)
    assert jobs == []
    assert mf.status is MediaStatus.SKIPPED_NO_GAIN  # type: ignore[attr-defined]
    assert mf.last_error.startswith("gain prévu insuffisant")  # type: ignore[attr-defined]
