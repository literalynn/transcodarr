"""Bibliothèques musique (core/audio.py) : cible, éligibilité, pochette,
commande FFmpeg — puis bout-en-bout sur un son synthétique avec pochette
(skippé sans ffmpeg/ffprobe, comme test_e2e_software.py)."""

from __future__ import annotations

import base64
import os
import shutil
import struct
import subprocess
from pathlib import Path
from typing import Any

import pytest

from transcoder import TOOL_ID
from transcoder.core.audio import (
    COVER_MIME,
    AudioTarget,
    build_audio_cmd,
    cover_stream,
    extract_cover,
    ffmetadata,
    gain_hopeless,
    picture_block,
    sample_rate,
    skip_reason,
    target_error,
)
from transcoder.core.dovi import TreatmentAction
from transcoder.core.probe import parse_probe, probe_file
from transcoder.core.queue import _command_change_predicate
from transcoder.core.replace import ReplaceError, replace_original
from transcoder.core.runner import run_ffmpeg
from transcoder.core.scanner import FileFacts, decide_status
from transcoder.core.strategy import Strategy
from transcoder.core.validate import ValidationOutcome, validate_audio_output
from transcoder.enums import AudioCodec, AudioContainer, MediaStatus
from transcoder.models import Job

OPUS_128 = AudioTarget(AudioCodec.OPUS, AudioContainer.OPUS, 128)


def _audio(index: int = 0, **overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "index": index,
        "codec_type": "audio",
        "codec_name": "flac",
        "sample_rate": "44100",
        "channels": 2,
        "tags": {},
    }
    base.update(overrides)
    return base


def _cover(index: int = 1) -> dict[str, Any]:
    return {
        "index": index,
        "codec_type": "video",
        "codec_name": "mjpeg",
        "width": 500,
        "height": 500,
        "disposition": {"attached_pic": 1},
    }


def _probe(streams: list[dict[str, Any]], **fmt_tags: str) -> Any:
    fmt = {"format_name": "flac", "duration": "200.0", "bit_rate": "900000", "tags": fmt_tags}
    return parse_probe("/m/Album/01.flac", {"format": fmt, "streams": streams})


# ---------------------------------------------------------------------------
# Cible, hash, marqueur anti-boucle
# ---------------------------------------------------------------------------
def test_target_validation() -> None:
    assert target_error(AudioCodec.OPUS, AudioContainer.OPUS, 128) is None
    assert target_error(AudioCodec.OPUS, AudioContainer.OGG, 510) is None
    assert "incompatible" in (target_error(AudioCodec.OPUS, AudioContainer.M4A, 128) or "")
    assert "hors limites" in (target_error(AudioCodec.MP3, AudioContainer.MP3, 400) or "")
    assert target_error(AudioCodec.AAC, AudioContainer.M4A, 31) is not None


def test_target_tag_and_hash() -> None:
    assert OPUS_128.tag == f"{TOOL_ID}|codec=opus|br=128|{OPUS_128.rule_hash}"
    others = {
        AudioTarget(AudioCodec.OPUS, AudioContainer.OGG, 128).rule_hash,
        AudioTarget(AudioCodec.OPUS, AudioContainer.OPUS, 96).rule_hash,
    }
    assert OPUS_128.rule_hash not in others and len(others) == 2


def test_tagged_music_file_is_done_until_target_changes() -> None:
    facts: dict[str, Any] = {
        "plan_action": TreatmentAction.SDR_PASSTHROUGH,
        "container_tag": OPUS_128.tag,
        "date_ok": True,
        "existing_status": None,
        "existing_applied_hash": None,
        "existing_retry_count": 0,
        "max_retries": 2,
    }
    done = decide_status(FileFacts(rule_hash=OPUS_128.rule_hash, **facts))
    assert done.status is MediaStatus.DONE
    changed = AudioTarget(AudioCodec.OPUS, AudioContainer.OPUS, 96).rule_hash
    redo = decide_status(FileFacts(rule_hash=changed, **facts))
    assert redo.status is MediaStatus.QUEUED and redo.rule_changed


def test_tag_readback_per_container() -> None:
    # Ogg : commentaire de la piste ; M4A : outil d'encodage (« encoder »)
    assert _probe([_audio(tags={"TRANSCODER_TAG": OPUS_128.tag})]).transcoder_tag == OPUS_128.tag
    assert _probe([_audio()], encoder=OPUS_128.tag).transcoder_tag == OPUS_128.tag
    assert _probe([_audio()], encoder="Lavf61.7.100").transcoder_tag is None


# ---------------------------------------------------------------------------
# Éligibilité et gain prévu
# ---------------------------------------------------------------------------
def test_skip_reason() -> None:
    with_cover = _probe([_audio(), _cover()])
    assert skip_reason(with_cover) is None  # une pochette n'est pas une piste vidéo
    assert with_cover.audio_bitrate_kbps == 900  # FLAC : débit global du fichier
    assert skip_reason(_probe([])) == "aucune piste audio"
    assert "2 pistes audio" in (skip_reason(_probe([_audio(), _audio(1)])) or "")
    video = {"index": 1, "codec_type": "video", "codec_name": "h264", "height": 720}
    assert "piste vidéo" in (skip_reason(_probe([_audio(), video])) or "")


def test_gain_hopeless() -> None:
    assert gain_hopeless(900, OPUS_128, 20.0) is None
    assert gain_hopeless(None, OPUS_128, 20.0) is None  # débit inconnu : on tente
    assert "gain prévu 0.0 %" in (gain_hopeless(128, OPUS_128, 20.0) or "")


# ---------------------------------------------------------------------------
# Pochette Ogg : bloc FLAC PICTURE via ffmetadata
# ---------------------------------------------------------------------------
def test_picture_block_layout() -> None:
    raw = base64.b64decode(picture_block(b"IMG", mime="image/png", width=2, height=3))
    pic_type, mime_len = struct.unpack(">II", raw[:8])
    assert (pic_type, raw[8 : 8 + mime_len]) == (3, b"image/png")
    off = 8 + mime_len + 4  # description vide
    assert struct.unpack(">IIIII", raw[off : off + 20]) == (2, 3, 24, 0, 3)
    assert raw[off + 20 :] == b"IMG"


def test_ffmetadata_escapes() -> None:
    assert ffmetadata({"T": "a=b;c#d\\e\nf"}) == ";FFMETADATA1\nT=a\\=b\\;c\\#d\\\\e\\\nf\n"


def test_cover_and_sample_rate() -> None:
    raw = {"streams": [_audio(sample_rate="96000"), _cover(1)]}
    assert cover_stream(raw) == _cover(1)
    assert sample_rate(raw) == 96000
    assert cover_stream({"streams": [_audio()]}) is None
    assert sample_rate({"streams": [{"codec_type": "audio"}]}) is None


# ---------------------------------------------------------------------------
# Commande FFmpeg
# ---------------------------------------------------------------------------
def _cmd(target: AudioTarget, **overrides: Any) -> list[str]:
    args: dict[str, Any] = {
        "ffmpeg_path": "ffmpeg",
        "src_path": "/m/a.flac",
        "dst_path": "/m/out",
        "target": target,
        "strategy": Strategy.STANDARD,
        "input_format": None,
        "cover_index": None,
        "metadata_path": None,
        "src_sample_rate": 44100,
        "src_channels": 2,
    }
    args.update(overrides)
    return build_audio_cmd(**args)


def _has(cmd: list[str], *seq: str) -> bool:
    return any(cmd[i : i + len(seq)] == list(seq) for i in range(len(cmd)))


def test_cmd_opus_cover_via_metadata_file() -> None:
    cmd = _cmd(OPUS_128, cover_index=1, metadata_path="/m/meta")
    assert _has(cmd, "-i", "/m/meta") and _has(cmd, "-map_metadata", "1")
    assert "-c:v" not in cmd  # le muxer Ogg refuse la pochette en flux
    assert _has(cmd, "-c:a", "libopus", "-b:a", "128k") and "-ar" not in cmd
    assert _has(cmd, "-metadata", f"TRANSCODER_TAG={OPUS_128.tag}") and cmd[-1] == "/m/out"


def test_cmd_mp3_hires_cover_stream() -> None:
    target = AudioTarget(AudioCodec.MP3, AudioContainer.MP3, 320)
    cmd = _cmd(target, cover_index=1, src_sample_rate=96000)
    assert _has(cmd, "-map", "0:1", "-c:v", "copy", "-disposition:v:0", "attached_pic")
    assert _has(cmd, "-c:a", "libmp3lame", "-b:a", "320k") and _has(cmd, "-ar", "48000")


def test_cmd_opus_multichannel_forces_vorbis_layout() -> None:
    # Production : FLAC 5 canaux en 5.0(side) → libopus EINVAL sans conversion
    assert _has(_cmd(OPUS_128, src_channels=5), "-af", "aformat=channel_layouts=5.0")
    assert _has(_cmd(OPUS_128, src_channels=6), "-af", "aformat=channel_layouts=5.1")
    assert "-af" not in _cmd(OPUS_128)  # stéréo : rien à convertir
    assert "-af" not in _cmd(OPUS_128, src_channels=None)
    aac = AudioTarget(AudioCodec.AAC, AudioContainer.M4A, 192)
    assert "-af" not in _cmd(aac, src_channels=6)  # l'encodeur AAC convertit seul


def test_cmd_m4a_tag_in_encoding_tool() -> None:
    target = AudioTarget(AudioCodec.AAC, AudioContainer.M4A, 192)
    cmd = _cmd(target)
    assert _has(cmd, "-metadata", f"encoding_tool={target.tag}")
    assert not any(a.startswith("TRANSCODER_TAG=") for a in cmd)


def test_music_fallback_is_tolerant_input_only() -> None:
    job = Job(diagnostics={"strategy": {"media_type": "music"}})
    first = _command_change_predicate(job, ["standard"])
    assert first is not None and first(Strategy.TOLERANT) and not first(Strategy.SAFE_STREAMS)
    after = _command_change_predicate(job, ["standard", "tolerant"])
    assert after is not None and not after(Strategy.TOLERANT)


def test_replace_never_overwrites_sibling(tmp_path: Path) -> None:
    src = tmp_path / "Album" / "01.flac"
    src.parent.mkdir()
    src.write_bytes(b"F" * 1000)
    sibling = src.with_suffix(".opus")
    sibling.write_bytes(b"autre")
    out = src.parent / ".transcoder_tmp" / "x.opus"
    out.parent.mkdir()
    out.write_bytes(b"N" * 100)
    with pytest.raises(ReplaceError, match="existe déjà"):
        replace_original(
            src_path=str(src),
            tmp_path=str(out),
            job_id=1,
            trash_dir=str(tmp_path / "trash"),
            immediate_delete=False,
            container=AudioContainer.OPUS,
        )
    assert src.read_bytes() == b"F" * 1000 and sibling.read_bytes() == b"autre"


# ---------------------------------------------------------------------------
# Bout-en-bout : FLAC + pochette → chaque cible, 7 contrôles
# ---------------------------------------------------------------------------
def _find_tool(binary: str) -> str | None:
    found = shutil.which(binary)
    if found:
        return found
    candidate = f"/usr/lib/jellyfin-ffmpeg/{binary}"
    return candidate if os.access(candidate, os.X_OK) else None


FFMPEG = _find_tool("ffmpeg")
FFPROBE = _find_tool("ffprobe")


@pytest.fixture
def flac_with_cover(tmp_path: Path) -> Path:
    assert FFMPEG is not None
    cover = tmp_path / "cover.png"
    src = tmp_path / "01.flac"
    subprocess.run(
        [FFMPEG, "-v", "error", "-f", "lavfi", "-i", "color=c=red:s=64x64",
         "-frames:v", "1", str(cover)],
        check=True, capture_output=True,
    )
    # Bruit rose : un FLAC qui ne se compresse pas (≈ 700 kbps), gain réel vers 128 kbps
    subprocess.run(
        [FFMPEG, "-v", "error", "-f", "lavfi", "-i", "anoisesrc=d=8:c=pink:r=44100:a=0.5",
         "-i", str(cover), "-map", "0:a", "-map", "1:v", "-ac", "2", "-c:a", "flac",
         "-c:v", "copy", "-disposition:v", "attached_pic", "-metadata", "TITLE=Piste",
         str(src)],
        check=True, capture_output=True,
    )
    return src


@pytest.mark.skipif(FFMPEG is None or FFPROBE is None, reason="ffmpeg/ffprobe absents")
@pytest.mark.parametrize(
    "target",
    [
        OPUS_128,
        AudioTarget(AudioCodec.AAC, AudioContainer.M4A, 128),
        AudioTarget(AudioCodec.MP3, AudioContainer.MP3, 128),
    ],
    ids=lambda t: t.container.value,
)
async def test_e2e_flac_with_cover(
    tmp_path: Path, flac_with_cover: Path, target: AudioTarget
) -> None:
    assert FFMPEG is not None and FFPROBE is not None
    src = flac_with_cover
    probe = await probe_file(str(src), FFPROBE, frames=False)
    assert skip_reason(probe) is None
    cover_info = cover_stream(probe.raw)
    assert cover_info is not None

    # Même préparation que QueueManager._prepare_audio
    metadata_path: str | None = None
    if target.container in (AudioContainer.OPUS, AudioContainer.OGG):
        data = await extract_cover(FFMPEG, str(src), int(cover_info["index"]))
        assert data is not None
        metadata_path = str(tmp_path / "meta.txt")
        Path(metadata_path).write_text(
            ffmetadata({"METADATA_BLOCK_PICTURE": picture_block(
                data,
                mime=COVER_MIME[cover_info["codec_name"]],
                width=int(cover_info["width"]),
                height=int(cover_info["height"]),
            )}),
            encoding="utf-8",
        )
    dst = tmp_path / f"out.{target.container.value}"
    cmd = build_audio_cmd(
        ffmpeg_path=FFMPEG,
        src_path=str(src),
        dst_path=str(dst),
        target=target,
        strategy=Strategy.STANDARD,
        input_format=None,
        cover_index=None if metadata_path else int(cover_info["index"]),
        metadata_path=metadata_path,
        src_sample_rate=sample_rate(probe.raw),
        src_channels=probe.audio_streams[0].get("channels"),
    )
    result = await run_ffmpeg(
        cmd, duration_s=probe.duration_s, log_path=str(tmp_path / "job.log")
    )
    assert result.exit_code == 0, result.log_tail

    report = await validate_audio_output(
        ffmpeg_path=FFMPEG,
        ffprobe_path=FFPROBE,
        src_probe=probe,
        src_size_bytes=src.stat().st_size,
        dst_path=str(dst),
        exit_code=result.exit_code,
        target=target,
        min_gain_percent=10.0,
    )
    assert report.outcome is ValidationOutcome.PASSED, report.failure_reason
    assert report.dst_probe is not None
    assert report.dst_probe.transcoder_tag == target.tag
    # Tags d'origine conservés (globaux en MP3/M4A, commentaires de piste en Ogg)
    raw = report.dst_probe.raw
    tags = [raw["format"].get("tags") or {}, *[s.get("tags") or {} for s in raw["streams"]]]
    assert any(k.upper() == "TITLE" and v == "Piste" for t in tags for k, v in t.items())


@pytest.fixture
def flac_5_0_side(tmp_path: Path) -> Path:
    assert FFMPEG is not None
    src = tmp_path / "01.flac"
    subprocess.run(
        [FFMPEG, "-v", "error", "-f", "lavfi", "-i", "anoisesrc=d=4:c=pink:r=48000:a=0.5",
         "-af", "pan=5.0(side)|c0=c0|c1=c0|c2=c0|c3=c0|c4=c0", "-c:a", "flac", str(src)],
        check=True, capture_output=True,
    )
    return src


@pytest.mark.skipif(FFMPEG is None or FFPROBE is None, reason="ffmpeg/ffprobe absents")
async def test_e2e_opus_from_5_0_side_flac(tmp_path: Path, flac_5_0_side: Path) -> None:
    """Disposition du FLAC 5 canaux (5.0(side)) refusée telle quelle par libopus."""
    assert FFMPEG is not None and FFPROBE is not None
    src = flac_5_0_side
    probe = await probe_file(str(src), FFPROBE, frames=False)
    assert probe.audio_streams[0]["channel_layout"] == "5.0(side)"
    dst = tmp_path / "out.opus"
    cmd = _cmd(
        OPUS_128, ffmpeg_path=FFMPEG, src_path=str(src), dst_path=str(dst),
        src_sample_rate=48000, src_channels=probe.audio_streams[0]["channels"],
    )
    result = await run_ffmpeg(cmd, duration_s=probe.duration_s, log_path=str(tmp_path / "log"))
    assert result.exit_code == 0, result.log_tail
    report = await validate_audio_output(
        ffmpeg_path=FFMPEG, ffprobe_path=FFPROBE, src_probe=probe,
        src_size_bytes=src.stat().st_size, dst_path=str(dst), exit_code=0,
        target=OPUS_128, min_gain_percent=10.0,
    )
    assert report.outcome is ValidationOutcome.PASSED, report.failure_reason
    assert report.dst_probe is not None
    assert report.dst_probe.audio_streams[0]["channels"] == 5  # canaux conservés
