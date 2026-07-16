"""Contrôles de validation §5.5 — logique pure, sans binaire."""

from __future__ import annotations

from typing import Any

import pytest

from transcoder.core.dovi import DvCapabilities, plan_treatment
from transcoder.core.probe import parse_probe
from transcoder.core.validate import (
    check_codec,
    check_colorimetry,
    check_duration,
    check_gain,
    check_no_dovi,
    check_stream_counts,
    parse_signalstats,
    parse_ssim_all,
    reference_decode_filter,
)
from transcoder.enums import Codec, DvPolicy, HdrFormat, HdrPolicy


def _probe(
    *,
    codec: str = "hevc",
    audio: int = 2,
    subs: int = 1,
    transfer: str | None = None,
    primaries: str | None = None,
    space: str | None = None,
    pix_fmt: str = "yuv420p10le",
    mdcv: bool = False,
    dv_profile: int | None = None,
) -> Any:
    streams: list[dict[str, Any]] = []
    side_data: list[dict[str, Any]] = []
    if mdcv:
        side_data += [
            {
                "side_data_type": "Mastering display metadata",
                "red_x": "35400/50000",
                "red_y": "14600/50000",
                "green_x": "8500/50000",
                "green_y": "39850/50000",
                "blue_x": "6550/50000",
                "blue_y": "2300/50000",
                "white_point_x": "15635/50000",
                "white_point_y": "16450/50000",
                "min_luminance": "1/10000",
                "max_luminance": "10000000/10000",
            },
            {
                "side_data_type": "Content light level metadata",
                "max_content": 1000,
                "max_average": 400,
            },
        ]
    if dv_profile is not None:
        side_data.append(
            {
                "side_data_type": "DOVI configuration record",
                "dv_profile": dv_profile,
                "dv_bl_signal_compatibility_id": 0,
                "el_present_flag": 0,
                "rpu_present_flag": 1,
            }
        )
    streams.append(
        {
            "index": 0,
            "codec_type": "video",
            "codec_name": codec,
            "width": 3840,
            "height": 2160,
            "pix_fmt": pix_fmt,
            "color_transfer": transfer,
            "color_primaries": primaries,
            "color_space": space,
            "side_data_list": side_data,
            "disposition": {},
            "tags": {},
        }
    )
    for i in range(audio):
        streams.append({"index": i + 1, "codec_type": "audio", "codec_name": "ac3"})
    for i in range(subs):
        streams.append({"index": audio + i + 1, "codec_type": "subtitle", "codec_name": "subrip"})
    return parse_probe(
        "/x/f.mkv",
        {"format": {"format_name": "matroska", "duration": "3600"}, "streams": streams},
    )


# --- Contrôle 3 : codec -----------------------------------------------------
def test_check_codec() -> None:
    assert check_codec(_probe(codec="hevc"), Codec.HEVC).ok
    assert not check_codec(_probe(codec="h264"), Codec.HEVC).ok
    assert check_codec(_probe(codec="av1"), Codec.AV1).ok


# --- Contrôle 4 : durée ------------------------------------------------------
@pytest.mark.parametrize(
    ("src", "dst", "ok"),
    [
        (3600.0, 3600.0, True),
        (3600.0, 3618.0, True),  # 0,5 % de 3600 = 18 s
        (3600.0, 3618.1, False),
        (100.0, 101.0, True),  # tolérance plancher : 1 s
        (100.0, 101.6, False),
        (None, 3600.0, False),
        (3600.0, None, False),
    ],
)
def test_check_duration(src: float | None, dst: float | None, ok: bool) -> None:
    assert check_duration(src, dst).ok is ok


# --- Contrôle 6 : complétude des flux ---------------------------------------
def test_check_stream_counts() -> None:
    assert check_stream_counts(_probe(audio=3, subs=2), _probe(audio=3, subs=2)).ok
    assert not check_stream_counts(_probe(audio=3, subs=2), _probe(audio=2, subs=2)).ok
    assert not check_stream_counts(_probe(audio=3, subs=2), _probe(audio=3, subs=1)).ok


# --- Contrôle 7 : gain -------------------------------------------------------
def test_check_gain_pass() -> None:
    check, gain, no_gain = check_gain(10_000_000_000, 6_000_000_000, 5.0)
    assert check.ok
    assert gain == 40.0
    assert not no_gain


def test_check_gain_below_threshold_keeps_original() -> None:
    _, gain, no_gain = check_gain(10_000_000_000, 9_700_000_000, 5.0)
    assert gain == pytest.approx(3.0)
    assert no_gain


def test_check_gain_output_bigger() -> None:
    _, gain, no_gain = check_gain(10_000_000_000, 12_000_000_000, 5.0)
    assert gain == -20.0
    assert no_gain


# --- Contrôle 8 : colorimétrie ----------------------------------------------
def _hdr_out(**overrides: Any) -> Any:
    defaults: dict[str, Any] = dict(
        transfer="smpte2084",
        primaries="bt2020",
        space="bt2020nc",
        pix_fmt="yuv420p10le",
        mdcv=True,
    )
    defaults.update(overrides)
    return _probe(**defaults)


def test_colorimetry_hdr10_ok() -> None:
    assert check_colorimetry(_hdr_out(), HdrFormat.HDR10).ok


@pytest.mark.parametrize(
    "bad",
    [
        {"primaries": "bt709"},
        {"transfer": "bt709"},
        {"space": "bt709"},
        {"pix_fmt": "yuv420p"},
        {"mdcv": False},
    ],
)
def test_colorimetry_hdr10_failures(bad: dict[str, Any]) -> None:
    assert not check_colorimetry(_hdr_out(**bad), HdrFormat.HDR10).ok


def test_colorimetry_hlg_expects_arib() -> None:
    hlg = _hdr_out(transfer="arib-std-b67", mdcv=False)
    assert check_colorimetry(hlg, HdrFormat.HLG).ok
    # Un HLG tagué PQ est une erreur (§5.3.1 : ne pas taguer PQ un signal HLG)
    assert not check_colorimetry(_hdr_out(mdcv=False), HdrFormat.HLG).ok


def test_no_dovi_in_output() -> None:
    assert check_no_dovi(_probe()).ok
    assert not check_no_dovi(_probe(dv_profile=8)).ok


# --- Contrôle 9 : parsers et référence de décodage ---------------------------
def test_parse_ssim_all() -> None:
    line = "[Parsed_ssim_0 @ 0x5b] SSIM Y:0.987 U:0.99 V:0.99 All:0.988123 (19.2)"
    assert parse_ssim_all(line) == pytest.approx(0.988123)
    assert parse_ssim_all("garbage") is None


def test_parse_signalstats() -> None:
    out = (
        "frame:0 pts:0 pts_time:0\n"
        "lavfi.signalstats.YAVG=81.23\n"
        "lavfi.signalstats.UAVG=128.44\n"
        "lavfi.signalstats.VAVG=127.91\n"
    )
    assert parse_signalstats(out) == (128.44, 127.91)
    assert parse_signalstats("rien") is None


def _plan_for(dv_profile: int, compat: int = 0) -> Any:
    p = _probe(transfer="smpte2084", dv_profile=dv_profile)
    # compat via re-probe : plus simple de repasser par parse_probe
    if compat:
        p = parse_probe(
            "/x/f.mkv",
            {
                "format": {"format_name": "matroska", "duration": "3600"},
                "streams": [
                    {
                        "index": 0,
                        "codec_type": "video",
                        "codec_name": "hevc",
                        "width": 3840,
                        "height": 2160,
                        "pix_fmt": "yuv420p10le",
                        "color_transfer": "smpte2084",
                        "side_data_list": [
                            {
                                "side_data_type": "DOVI configuration record",
                                "dv_profile": dv_profile,
                                "dv_bl_signal_compatibility_id": compat,
                                "el_present_flag": 0,
                                "rpu_present_flag": 1,
                            }
                        ],
                        "disposition": {},
                        "tags": {},
                    }
                ],
            },
        )
    return plan_treatment(
        p,
        hdr_policy=HdrPolicy.PRESERVE,
        dv_policy=DvPolicy.CONVERT_TO_HDR10,
        caps=DvCapabilities(libplacebo_dv=True, libplacebo=True),
    )


def test_reference_decode_p5_applies_rpu() -> None:
    """Arbitrage 2 : P5 → référence = source décodée AVEC RPU (libplacebo)."""
    ref = reference_decode_filter(_plan_for(5))
    assert ref is not None
    assert "apply_dolbyvision=true" in ref


def test_reference_decode_p7_p81_without_rpu() -> None:
    """Arbitrage 2 : P7/P8.1 → référence = BL décodée SANS RPU (pas de faux positifs)."""
    assert reference_decode_filter(_plan_for(7, compat=6)) is None
    assert reference_decode_filter(_plan_for(8, compat=1)) is None


def test_stream_counts_ignore_unknown_codec_tracks() -> None:
    """Les pistes au codec inconnu (exclues du mapping par le builder) ne
    comptent pas dans la complétude attendue (§5.5.6)."""
    src = _probe(audio=2, subs=1)
    # Ajoute une piste de sous-titres SANS codec à la source
    raw = {
        "format": {"format_name": "matroska", "duration": "3600"},
        "streams": [
            {"index": 0, "codec_type": "video", "codec_name": "hevc", "width": 1920,
             "height": 1080, "pix_fmt": "yuv420p", "disposition": {}, "tags": {}},
            {"index": 1, "codec_type": "audio", "codec_name": "ac3"},
            {"index": 2, "codec_type": "audio", "codec_name": "aac"},
            {"index": 3, "codec_type": "subtitle", "codec_name": "subrip"},
            {"index": 4, "codec_type": "subtitle"},  # codec inconnu
        ],
    }
    src_with_broken = parse_probe("/x/src.mkv", raw)
    dst = _probe(audio=2, subs=1)  # la piste cassée n'est pas dans la sortie
    assert check_stream_counts(src_with_broken, dst).ok
    # Mais une piste VALIDE manquante reste un échec
    dst_missing = _probe(audio=1, subs=1)
    assert not check_stream_counts(src_with_broken, dst_missing).ok
