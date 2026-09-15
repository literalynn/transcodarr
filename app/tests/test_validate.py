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


# ---------------------------------------------------------------------------
# Contrôle 5 : interprétation du décodage d'intégrité (vidéo seule)
# ---------------------------------------------------------------------------
def test_integrity_result_clean() -> None:
    from transcoder.core.validate import integrity_result

    assert integrity_result(0, "", scope="décodage").ok


def test_integrity_result_benign_noise_passes() -> None:
    from transcoder.core.validate import integrity_result

    noise = "[null @ 0x1] Application provided invalid, non monotonically increasing dts to muxer\n"
    assert integrity_result(0, noise, scope="décodage").ok


def test_integrity_result_crash_without_message() -> None:
    """Cas prod « 5_integrity : erreurs : » (code ≠ 0, stderr vide)."""
    from transcoder.core.validate import integrity_result

    r = integrity_result(-11, "", scope="décodage")
    assert not r.ok and r.meta is not None and r.meta["crashed"]
    assert "SIGSEGV" in r.detail
    r2 = integrity_result(1, "", scope="décodage")
    assert not r2.ok and r2.meta is not None and r2.meta["crashed"]


def test_integrity_result_real_errors() -> None:
    from transcoder.core.validate import integrity_result

    r = integrity_result(183, "[hevc @ 0x2] Invalid NAL unit size\n", scope="décodage")
    assert not r.ok and r.meta is not None and not r.meta["crashed"]
    assert "Invalid NAL unit size" in r.detail


def test_stream_counts_use_stream_plan() -> None:
    from transcoder.core.stream_plan import plan_streams
    from transcoder.enums import Container

    src = _probe(audio=2, subs=1)
    raw_streams = src.raw["streams"] + [{"index": 9, "codec_type": "data"}]
    plan = plan_streams(raw_streams, container=Container.MKV)
    dst = _probe(audio=2, subs=1)
    assert check_stream_counts(src, dst, stream_plan=plan).ok
    dst_missing = _probe(audio=1, subs=1)
    assert not check_stream_counts(src, dst_missing, stream_plan=plan).ok


# ---------------------------------------------------------------------------
# Contrôle 6b : horodatages des pistes copiées (log RÉEL de production)
# ---------------------------------------------------------------------------
PROD_LOG_ASS_SHIFTED = """
[matroska,webm @ 0x55b3868db040] Could not find codec parameters for stream 20 (Attachment: none): unknown codec
[sost#0:3/copy @ 0x55b38ffaf640] Non-monotonic DTS; previous: 1248000, current: 15000; changing to 1248000. This may result in incorrect timestamps in the output file.
[sost#0:4/copy @ 0x55b38ffb0340] Non-monotonic DTS; previous: 1380980, current: 15000; changing to 1380980. This may result in incorrect timestamps in the output file.
[sost#0:4/copy @ 0x55b38ffb0340] Non-monotonic DTS; previous: 1380980, current: 210140; changing to 1380980. This may result in incorrect timestamps in the output file.
"""

PGS_ONE_MS = """
[sost#0:5/copy @ 0x1] Non-monotonic DTS; previous: 845612, current: 845512; changing to 845612. This may result in incorrect timestamps in the output file.
[sost#0:5/copy @ 0x1] Non-monotonic DTS; previous: 845612, current: 845600; changing to 845612. This may result in incorrect timestamps in the output file.
"""


def test_parse_dts_corrections_real_log() -> None:
    from transcoder.core.validate import parse_dts_corrections

    c = parse_dts_corrections(PROD_LOG_ASS_SHIFTED)
    assert c.count == {"s": 3}
    assert c.max_shift_ms["s"] == 1380980 - 15000
    assert c.worst_stream["s"] == 4


def test_shifted_ass_events_fail_validation() -> None:
    from transcoder.core.failures import FailureCategory, classify_validation_failure
    from transcoder.core.validate import check_copied_timestamps

    r = check_copied_timestamps(PROD_LOG_ASS_SHIFTED, matroska_output=True)
    assert not r.ok
    assert "sous-titres" in r.detail and "1365.98 s" in r.detail
    f = classify_validation_failure(
        r.name, r.detail, exit_code=0, ffmpeg_log="", check_meta=r.meta
    )
    assert f.category is FailureCategory.SUBTITLE_TIMING


# Log RÉEL (film WEB-DL 2160p) : trous d'images de 6 s dans la source,
# l'encodeur QSV sort 2 DTS en recul à chaque trou
WEBDL_VIDEO_GAPS = "".join(
    f"[vost#0:0/hevc_qsv @ 0x56053e208f80] Non-monotonic DTS; previous: {p}, current: {p - s}; "
    f"changing to {p}. This may result in incorrect timestamps in the output file.\n"
    for p in (588505, 2286201, 2652567, 2778693, 3145059, 3631545, 3751581)
    for s in (5965, 5923)
)


def test_encoded_video_corrections_at_source_gaps_are_tolerated() -> None:
    from transcoder.core.validate import check_copied_timestamps

    r = check_copied_timestamps(WEBDL_VIDEO_GAPS, matroska_output=True, duration_s=6060)
    assert r.ok, r.detail
    assert r.meta is not None and r.meta["corrections"] == {"v": 14}


def test_widespread_video_corrections_still_fail() -> None:
    from transcoder.core.validate import check_copied_timestamps, video_corrections_allowed

    allowed = video_corrections_allowed(1440)  # épisode de 24 min
    assert allowed == 172
    line = (
        "[vost#0:0/av1_qsv @ 0x1] Non-monotonic DTS; previous: 90000, current: 80000; "
        "changing to 90000. This may result in incorrect timestamps in the output file.\n"
    )
    assert not check_copied_timestamps(line * (allowed + 1), matroska_output=True, duration_s=1440).ok


def test_video_tolerance_never_hides_shifted_subtitles() -> None:
    from transcoder.core.validate import check_copied_timestamps

    r = check_copied_timestamps(
        WEBDL_VIDEO_GAPS + PROD_LOG_ASS_SHIFTED, matroska_output=True, duration_s=6060
    )
    assert not r.ok
    assert "sous-titres" in r.detail and "vidéo" not in r.detail


def test_tiny_corrections_tolerated_in_matroska() -> None:
    from transcoder.core.validate import check_copied_timestamps

    r = check_copied_timestamps(PGS_ONE_MS, matroska_output=True)
    assert r.ok and "négligeables" in r.detail
    assert not check_copied_timestamps(PGS_ONE_MS, matroska_output=False).ok
    assert check_copied_timestamps("", matroska_output=True).ok
    assert check_copied_timestamps(None, matroska_output=True).ok
