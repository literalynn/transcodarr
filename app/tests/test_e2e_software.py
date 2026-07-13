"""Intégration bout-en-bout sur mire FFmpeg, en encodeurs LOGICIELS (§9.6).

Aucun GPU requis : la mire est générée en libx264 et réencodée en libx265
(ou libsvtav1/libaom en repli). Si aucun ffmpeg n'est disponible dans le
PATH, tous les tests sont skippés proprement (environnement de dev).

Couvre en réel : sonde ffprobe → runner (progression + logs) → les 9
validations §5.5 (dont décodage d'intégrité) → tag anti-boucle §5.7.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from transcoder.core.dovi import DvCapabilities, plan_treatment
from transcoder.core.probe import probe_file
from transcoder.core.rules import ResolvedRule, build_transcoder_tag, compute_rule_hash
from transcoder.core.runner import ProgressUpdate, run_ffmpeg
from transcoder.core.validate import ValidationOutcome, validate_output
from transcoder.enums import (
    Codec,
    Container,
    DvPolicy,
    HdrFormat,
    HdrPolicy,
    ResolutionBucket,
)

def _find_tool(binary: str) -> str | None:
    """Cherche dans le PATH puis à l'emplacement jellyfin-ffmpeg (conteneur)."""
    found = shutil.which(binary)
    if found:
        return found
    candidate = f"/usr/lib/jellyfin-ffmpeg/{binary}"
    return candidate if os.access(candidate, os.X_OK) else None

FFMPEG = _find_tool("ffmpeg")
FFPROBE = _find_tool("ffprobe")

pytestmark = pytest.mark.skipif(
    FFMPEG is None or FFPROBE is None,
    reason="ffmpeg/ffprobe absents du PATH : e2e logicielle skippée",
)


def _encoders() -> set[str]:
    assert FFMPEG is not None
    out = subprocess.run(
        [FFMPEG, "-hide_banner", "-encoders"], capture_output=True, text=True, check=True
    ).stdout
    return {line.split()[1] for line in out.splitlines() if line.startswith(" V")}


def _software_encoder(target: Codec) -> str | None:
    encs = _encoders()
    if target is Codec.HEVC:
        return "libx265" if "libx265" in encs else None
    for cand in ("libsvtav1", "libaom-av1"):
        if cand in encs:
            return cand
    return None


@pytest.fixture(scope="module")
def source_file(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Mire 6 s 640x360 x264 haut bitrate + audio sinus (stéréo)."""
    assert FFMPEG is not None
    path = tmp_path_factory.mktemp("e2e") / "source.mkv"
    subprocess.run(
        [
            FFMPEG, "-hide_banner", "-nostdin", "-y", "-v", "error",
            "-f", "lavfi", "-i", "testsrc2=duration=6:size=640x360:rate=25",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=6",
            "-c:v", "libx264", "-b:v", "2000k", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "128k",
            "-metadata", "title=mire e2e",
            str(path),
        ],
        check=True,
    )
    return path


def _rule(codec: Codec) -> ResolvedRule:
    return ResolvedRule(
        codec=codec,
        bucket=ResolutionBucket.P480,
        is_hdr=False,
        target_kbps=300,
        max_kbps=450,
        bufsize_kbps=600,
        rule_hash=compute_rule_hash(
            codec=codec,
            bucket=ResolutionBucket.P480,
            is_hdr=False,
            target_kbps=300,
            max_kbps=450,
            bufsize_kbps=600,
            container=Container.MKV,
            hdr_policy=HdrPolicy.PRESERVE,
            dv_policy=DvPolicy.CONVERT_TO_HDR10,
        ),
    )


async def test_probe_real_file(source_file: Path) -> None:
    assert FFPROBE is not None
    probe = await probe_file(str(source_file), FFPROBE)
    assert probe.video_codec == "h264"
    assert probe.width == 640 and probe.height == 360
    assert probe.resolution_bucket is ResolutionBucket.P480
    assert probe.duration_s is not None and abs(probe.duration_s - 6.0) < 0.5
    assert probe.hdr_format is HdrFormat.SDR
    assert len(probe.audio_streams) == 1
    assert probe.transcoder_tag is None


async def test_full_pipeline_encode_validate_tag(
    source_file: Path, tmp_path: Path
) -> None:
    """Encode réellement, valide (§5.5), vérifie le tag anti-boucle (§5.7)."""
    assert FFMPEG is not None and FFPROBE is not None
    encoder = _software_encoder(Codec.HEVC)
    if encoder is None:
        pytest.skip("libx265 absent de ce ffmpeg")

    rule = _rule(Codec.HEVC)
    tag = build_transcoder_tag(rule)
    dst = tmp_path / "out.mkv"
    # Équivalent logiciel de la commande du builder (même squelette §5.3) :
    # seul l'encodeur diffère (libx265 au lieu de hevc_qsv)
    cmd = [
        FFMPEG, "-hide_banner", "-nostdin", "-y",
        "-i", str(source_file),
        "-map", "0", "-map", "-0:v", "-map", "0:v:0",
        "-c", "copy",
        "-c:v", encoder, "-preset", "ultrafast",
        "-b:v", f"{rule.target_kbps}k",
        "-maxrate", f"{rule.max_kbps}k",
        "-bufsize", f"{rule.bufsize_kbps}k",
        "-pix_fmt", "yuv420p",
        "-map_metadata", "0", "-map_chapters", "0",
        "-metadata", f"TRANSCODER_TAG={tag}",
        "-progress", "pipe:1", "-nostats", "-loglevel", "warning",
        str(dst),
    ]

    src_probe = await probe_file(str(source_file), FFPROBE)
    updates: list[ProgressUpdate] = []

    async def on_progress(u: ProgressUpdate) -> None:
        updates.append(u)

    result = await run_ffmpeg(
        cmd,
        duration_s=src_probe.duration_s,
        log_path=str(tmp_path / "job.log"),
        on_progress=on_progress,
    )
    assert result.exit_code == 0
    assert updates, "aucun bloc de progression parsé"
    assert updates[-1].finished

    plan = plan_treatment(
        src_probe,
        hdr_policy=HdrPolicy.PRESERVE,
        dv_policy=DvPolicy.CONVERT_TO_HDR10,
        caps=DvCapabilities(libplacebo_dv=False, libplacebo=False),
    )
    report = await validate_output(
        ffmpeg_path=FFMPEG,
        ffprobe_path=FFPROBE,
        src_probe=src_probe,
        src_size_bytes=source_file.stat().st_size,
        dst_path=str(dst),
        exit_code=result.exit_code,
        plan=plan,
        target_codec=Codec.HEVC,
        min_gain_percent=5.0,
        validation_decode="full",
        dv_ssim_threshold=0.90,
        dv_chroma_delta_max=8.0,
        tmp_dir=str(tmp_path),
    )
    assert report.outcome is ValidationOutcome.PASSED, report.as_dict()
    check_names = {c.name for c in report.checks}
    # SDR : colorimétrie HDR non exigée, mais l'absence de DOVI est vérifiée
    assert {"1_exit_code", "2_output_exists", "3_codec", "4_duration",
            "5_integrity", "6_streams", "7_gain", "8b_no_dovi"} <= check_names

    # Marqueur anti-boucle : la sortie porte le tag, relisible par la sonde
    dst_probe = await probe_file(str(dst), FFPROBE)
    assert dst_probe.transcoder_tag == tag
    assert dst_probe.video_codec == "hevc"
    assert len(dst_probe.audio_streams) == 1  # audio copié tel quel


async def test_validation_rejects_corrupt_output(source_file: Path, tmp_path: Path) -> None:
    """Un fichier tronqué/corrompu ne passe JAMAIS la validation (§5.5.5)."""
    assert FFMPEG is not None and FFPROBE is not None
    encoder = _software_encoder(Codec.HEVC)
    if encoder is None:
        pytest.skip("libx265 absent de ce ffmpeg")
    dst = tmp_path / "corrupt.mkv"
    subprocess.run(
        [
            FFMPEG, "-hide_banner", "-nostdin", "-y", "-v", "error",
            "-i", str(source_file),
            "-map", "0", "-map", "-0:v", "-map", "0:v:0",
            "-c", "copy", "-c:v", encoder, "-preset", "ultrafast", "-b:v", "300k",
            str(dst),
        ],
        check=True,
    )
    # Troncature brutale : simule un crash/écriture partielle
    data = dst.read_bytes()
    dst.write_bytes(data[: len(data) // 2])

    src_probe = await probe_file(str(source_file), FFPROBE)
    plan = plan_treatment(
        src_probe,
        hdr_policy=HdrPolicy.PRESERVE,
        dv_policy=DvPolicy.CONVERT_TO_HDR10,
        caps=DvCapabilities(libplacebo_dv=False, libplacebo=False),
    )
    report = await validate_output(
        ffmpeg_path=FFMPEG,
        ffprobe_path=FFPROBE,
        src_probe=src_probe,
        src_size_bytes=source_file.stat().st_size,
        dst_path=str(dst),
        exit_code=0,
        plan=plan,
        target_codec=Codec.HEVC,
        min_gain_percent=5.0,
        validation_decode="full",
        dv_ssim_threshold=0.90,
        dv_chroma_delta_max=8.0,
        tmp_dir=str(tmp_path),
    )
    assert report.outcome is ValidationOutcome.FAILED
    assert report.failure_reason is not None


async def test_no_gain_keeps_original_semantics(source_file: Path, tmp_path: Path) -> None:
    """Sortie plus grosse que la source → NO_GAIN (§5.5.7), jamais PASSED."""
    assert FFMPEG is not None and FFPROBE is not None
    encoder = _software_encoder(Codec.HEVC)
    if encoder is None:
        pytest.skip("libx265 absent de ce ffmpeg")
    dst = tmp_path / "big.mkv"
    subprocess.run(
        [
            FFMPEG, "-hide_banner", "-nostdin", "-y", "-v", "error",
            "-i", str(source_file),
            "-map", "0", "-map", "-0:v", "-map", "0:v:0",
            "-c", "copy", "-c:v", encoder, "-preset", "ultrafast",
            "-b:v", "8000k",  # plus gros que la source
            str(dst),
        ],
        check=True,
    )
    src_probe = await probe_file(str(source_file), FFPROBE)
    plan = plan_treatment(
        src_probe,
        hdr_policy=HdrPolicy.PRESERVE,
        dv_policy=DvPolicy.CONVERT_TO_HDR10,
        caps=DvCapabilities(libplacebo_dv=False, libplacebo=False),
    )
    report = await validate_output(
        ffmpeg_path=FFMPEG,
        ffprobe_path=FFPROBE,
        src_probe=src_probe,
        src_size_bytes=source_file.stat().st_size,
        dst_path=str(dst),
        exit_code=0,
        plan=plan,
        target_codec=Codec.HEVC,
        min_gain_percent=5.0,
        validation_decode="sample",
        dv_ssim_threshold=0.90,
        dv_chroma_delta_max=8.0,
        tmp_dir=str(tmp_path),
    )
    assert report.outcome is ValidationOutcome.NO_GAIN


async def test_cancellation_kills_ffmpeg(tmp_path: Path) -> None:
    """SIGTERM propre sur une vraie instance FFmpeg (§5.2)."""
    assert FFMPEG is not None
    encoder = _software_encoder(Codec.HEVC)
    if encoder is None:
        pytest.skip("libx265 absent de ce ffmpeg")
    cmd = [
        FFMPEG, "-hide_banner", "-nostdin", "-y", "-v", "error",
        "-f", "lavfi", "-i", "testsrc2=duration=3600:size=640x360:rate=25",
        "-c:v", encoder, "-preset", "ultrafast", "-b:v", "300k",
        "-progress", "pipe:1", "-nostats",
        str(tmp_path / "cancel.mkv"),
    ]
    cancel = asyncio.Event()

    async def trigger() -> None:
        await asyncio.sleep(1.0)
        cancel.set()

    task = asyncio.create_task(trigger())
    result = await run_ffmpeg(
        cmd, duration_s=3600.0, log_path=str(tmp_path / "c.log"), cancel_event=cancel
    )
    await task
    assert result.cancelled
    assert result.exit_code != 0
