"""Détection de capacités : parsers purs + faux binaires (sans GPU)."""

from __future__ import annotations

import stat
import sys
from pathlib import Path

from transcoder.core.capabilities import (
    GpuConfig,
    detect_capabilities,
    parse_ffmpeg_version,
    parse_libplacebo_help,
    probe_gpu,
    run_encoder_test,
)
from transcoder.enums import Codec, GpuApi


def test_parse_ffmpeg_version() -> None:
    out = "ffmpeg version 7.0.2-Jellyfin Copyright (c) 2000-2024"
    assert parse_ffmpeg_version(out) == "7.0.2-Jellyfin"
    assert parse_ffmpeg_version("garbage") is None


def test_parse_libplacebo_help() -> None:
    with_dv = (
        "Filter libplacebo\n  apply_dolbyvision <boolean> ..FVA...... Apply Dolby Vision "
        "metadata (default true)\n"
    )
    assert parse_libplacebo_help(with_dv) == (True, True)
    without_dv = "Filter libplacebo\n  tonemapping <int> ...\n"
    assert parse_libplacebo_help(without_dv) == (True, False)
    assert parse_libplacebo_help("Unknown filter 'libplacebo'.") == (False, False)


def _fake_bin(tmp_path: Path, name: str, body: str) -> str:
    script = tmp_path / name
    script.write_text(f"#!{sys.executable}\nimport sys\n{body}\n", encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script)


# Faux ffmpeg : -version OK, libplacebo avec DV, encodeurs QSV KO / VAAPI OK
_FFMPEG_QSV_BROKEN = """
args = " ".join(sys.argv[1:])
if "-version" in args:
    print("ffmpeg version 7.0-test"); sys.exit(0)
if "filter=libplacebo" in args:
    print("Filter libplacebo"); print("  apply_dolbyvision <boolean>"); sys.exit(0)
if "_qsv" in args:
    print("MFX session init failed", file=sys.stderr); sys.exit(1)
if "_vaapi" in args and "libplacebo" not in args:
    sys.exit(0)
if "libplacebo" in args:
    sys.exit(1)
sys.exit(0)
"""


async def test_encoder_ok_and_ko(tmp_path: Path) -> None:
    ffmpeg = _fake_bin(tmp_path, "ffmpeg", _FFMPEG_QSV_BROKEN)
    ok = await run_encoder_test(
        ffmpeg, codec=Codec.HEVC, api=GpuApi.VAAPI, drm_node="/dev/dri/renderD128"
    )
    assert ok.ok
    ko = await run_encoder_test(
        ffmpeg, codec=Codec.HEVC, api=GpuApi.QSV, drm_node="/dev/dri/renderD128"
    )
    assert not ko.ok
    assert ko.error is not None and "MFX" in ko.error


async def test_probe_gpu_falls_back_to_vaapi(tmp_path: Path) -> None:
    """§5.3.4 : QSV KO au démarrage → fallback automatique VAAPI, loggé."""
    ffmpeg = _fake_bin(tmp_path, "ffmpeg", _FFMPEG_QSV_BROKEN)
    gpu = GpuConfig(id=1, name="Arc A770", drm_node="/dev/dri/renderD128", api=GpuApi.QSV)
    cap = await probe_gpu(ffmpeg, gpu, libplacebo=True)
    assert cap.fell_back_to_vaapi
    assert cap.effective_api is GpuApi.VAAPI
    assert cap.supports(Codec.HEVC)
    # Chaîne zéro-copie KO dans ce faux ffmpeg → repli logiciel
    assert not cap.p5_zero_copy


async def test_detect_capabilities_all_missing(tmp_path: Path) -> None:
    caps = await detect_capabilities(
        ffmpeg_path=str(tmp_path / "absent-ffmpeg"),
        ffprobe_path=str(tmp_path / "absent-ffprobe"),
        dovi_tool_path=str(tmp_path / "absent-dovi"),
        mkvpropedit_path=str(tmp_path / "absent-mkvpropedit"),
        gpus=[GpuConfig(id=1, name="x", drm_node="/dev/dri/renderD128", api=GpuApi.QSV)],
    )
    assert not caps.ffmpeg_ok
    assert not caps.libplacebo_dv
    assert not caps.dovi_tool_ok
    # Sans ffmpeg, aucun test GPU n'est tenté
    assert caps.gpus == []
    # Le refus du P5 découle des capacités
    assert not caps.dv_caps.libplacebo_dv


async def test_detect_capabilities_nominal(tmp_path: Path) -> None:
    ffmpeg = _fake_bin(
        tmp_path,
        "ffmpeg",
        """
args = " ".join(sys.argv[1:])
if "-version" in args:
    print("ffmpeg version 7.0-test"); sys.exit(0)
if "filter=libplacebo" in args:
    print("Filter libplacebo"); print("  apply_dolbyvision <boolean>"); sys.exit(0)
sys.exit(0)
""",
    )
    ok_bin = _fake_bin(tmp_path, "okbin", "sys.exit(0)")
    caps = await detect_capabilities(
        ffmpeg_path=ffmpeg,
        ffprobe_path=ok_bin,
        dovi_tool_path=ok_bin,
        mkvpropedit_path=ok_bin,
        gpus=[GpuConfig(id=1, name="Arc", drm_node="/dev/dri/renderD128", api=GpuApi.QSV)],
    )
    assert caps.ffmpeg_ok
    assert caps.ffmpeg_version == "7.0-test"
    assert caps.libplacebo_dv
    assert caps.dovi_tool_ok
    assert caps.mkvpropedit_ok
    assert len(caps.gpus) == 1
    gpu = caps.gpus[0]
    assert not gpu.fell_back_to_vaapi
    assert gpu.supports(Codec.HEVC) and gpu.supports(Codec.AV1)
    assert gpu.p5_zero_copy
    d = caps.as_dict()
    assert d["gpus"][0]["encoders"]["hevc_qsv"]["ok"] is True
