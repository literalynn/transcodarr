"""Parsing pur des JSON ffprobe → MediaProbe (sans binaire ffprobe)."""

from __future__ import annotations

from typing import Any

from transcoder.core.probe import parse_probe
from transcoder.enums import HdrFormat, ResolutionBucket


def _video_stream(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "index": 0,
        "codec_type": "video",
        "codec_name": "h264",
        "width": 1920,
        "height": 1080,
        "pix_fmt": "yuv420p",
        "bit_rate": "8000000",
        "disposition": {"default": 1, "attached_pic": 0},
        "tags": {},
    }
    base.update(overrides)
    return base


def _fmt(streams: list[dict[str, Any]], **fmt_overrides: Any) -> dict[str, Any]:
    fmt: dict[str, Any] = {
        "format_name": "matroska,webm",
        "duration": "5400.123",
        "tags": {},
    }
    fmt.update(fmt_overrides)
    return {"format": fmt, "streams": streams, "chapters": []}


def test_sdr_h264_basic() -> None:
    p = parse_probe("/x/film.mkv", _fmt([_video_stream()]))
    assert p.video_codec == "h264"
    assert p.width == 1920 and p.height == 1080
    assert p.resolution_bucket is ResolutionBucket.P1080
    assert p.video_bitrate_kbps == 8000
    assert p.bit_depth == 8
    assert p.hdr_format is HdrFormat.SDR
    assert not p.is_hdr
    assert p.duration_s == 5400.123
    assert p.transcoder_tag is None


def test_bitrate_from_bps_tag() -> None:
    s = _video_stream(bit_rate=None, tags={"BPS-eng": "6500000"})
    p = parse_probe("/x/f.mkv", _fmt([s]))
    assert p.video_bitrate_kbps == 6500


def test_hdr10_with_mastering_metadata() -> None:
    s = _video_stream(
        codec_name="hevc",
        width=3840,
        height=2160,
        pix_fmt="yuv420p10le",
        color_primaries="bt2020",
        color_transfer="smpte2084",
        color_space="bt2020nc",
        side_data_list=[
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
                "min_luminance": "50/10000",
                "max_luminance": "10000000/10000",
            },
            {
                "side_data_type": "Content light level metadata",
                "max_content": 1000,
                "max_average": 400,
            },
        ],
    )
    p = parse_probe("/x/f.mkv", _fmt([s]))
    assert p.hdr_format is HdrFormat.HDR10
    assert p.is_hdr
    assert p.bit_depth == 10
    assert p.resolution_bucket is ResolutionBucket.P2160
    assert p.mastering_display is not None
    assert abs(p.mastering_display.red_x - 0.708) < 1e-6
    assert p.mastering_display.max_luminance == 1000.0
    assert p.mastering_display.min_luminance == 0.005
    assert p.content_light is not None
    assert p.content_light.max_cll == 1000
    assert p.content_light.max_fall == 400


def test_hlg_detection() -> None:
    s = _video_stream(codec_name="hevc", color_transfer="arib-std-b67", pix_fmt="yuv420p10le")
    p = parse_probe("/x/f.mkv", _fmt([s]))
    assert p.hdr_format is HdrFormat.HLG
    assert p.is_hdr


def test_dolby_vision_p5_from_stream_side_data() -> None:
    s = _video_stream(
        codec_name="hevc",
        width=3840,
        height=2160,
        pix_fmt="yuv420p10le",
        color_transfer="smpte2084",
        side_data_list=[
            {
                "side_data_type": "DOVI configuration record",
                "dv_profile": 5,
                "dv_bl_signal_compatibility_id": 0,
                "el_present_flag": 0,
                "rpu_present_flag": 1,
            }
        ],
    )
    p = parse_probe("/x/f.mkv", _fmt([s]))
    assert p.hdr_format is HdrFormat.DOLBY_VISION
    assert p.dv_profile == 5
    assert p.dv_bl_compat_id == 0
    assert p.dv_has_el is False
    assert p.dv_has_rpu is True
    # P5 : pas de MDCV statique exploitable
    assert p.mastering_display is None


def test_dolby_vision_from_first_frame_side_data() -> None:
    """Le DOVI record peut n'apparaître que sur la première frame."""
    s = _video_stream(codec_name="hevc", color_transfer="smpte2084", pix_fmt="yuv420p10le")
    frames = {
        "frames": [
            {
                "side_data_list": [
                    {
                        "side_data_type": "DOVI configuration record",
                        "dv_profile": 8,
                        "dv_bl_signal_compatibility_id": 1,
                        "el_present_flag": 0,
                        "rpu_present_flag": 1,
                    }
                ]
            }
        ]
    }
    p = parse_probe("/x/f.mkv", _fmt([s]), frames)
    assert p.hdr_format is HdrFormat.DOLBY_VISION
    assert p.dv_profile == 8
    assert p.dv_bl_compat_id == 1


def test_hdr10plus_detection() -> None:
    s = _video_stream(codec_name="hevc", color_transfer="smpte2084", pix_fmt="yuv420p10le")
    frames = {
        "frames": [
            {
                "side_data_list": [
                    {"side_data_type": "HDR Dynamic Metadata SMPTE2094-40 (HDR10+)"}
                ]
            }
        ]
    }
    p = parse_probe("/x/f.mkv", _fmt([s]), frames)
    assert p.hdr_format is HdrFormat.HDR10PLUS


def test_dv_p7_dual_layer_counts_two_video_streams() -> None:
    bl = _video_stream(
        codec_name="hevc",
        width=3840,
        height=2160,
        pix_fmt="yuv420p10le",
        color_transfer="smpte2084",
        side_data_list=[
            {
                "side_data_type": "DOVI configuration record",
                "dv_profile": 7,
                "dv_bl_signal_compatibility_id": 6,
                "el_present_flag": 1,
                "rpu_present_flag": 1,
            }
        ],
    )
    el = _video_stream(index=1, codec_name="hevc", width=1920, height=1080)
    p = parse_probe("/x/f.mkv", _fmt([bl, el]))
    assert p.dv_profile == 7
    assert p.dv_has_el is True
    assert p.video_stream_count == 2
    # La sonde décrit la PREMIÈRE piste vidéo (la BL)
    assert p.width == 3840


def test_streams_and_tag_inventory() -> None:
    audio1 = {
        "index": 1,
        "codec_type": "audio",
        "codec_name": "truehd",
        "channels": 8,
        "tags": {"language": "fre", "title": "VF Atmos"},
        "disposition": {"default": 1},
    }
    audio2 = {
        "index": 2,
        "codec_type": "audio",
        "codec_name": "ac3",
        "channels": 6,
        "tags": {"language": "eng"},
        "disposition": {"default": 0},
    }
    sub = {
        "index": 3,
        "codec_type": "subtitle",
        "codec_name": "subrip",
        "tags": {"language": "fre"},
        "disposition": {"default": 0, "forced": 1},
    }
    font = {"index": 4, "codec_type": "attachment", "codec_name": "ttf"}
    data = {"index": 5, "codec_type": "data", "codec_name": "bin_data"}
    doc = _fmt(
        [_video_stream(), audio1, audio2, sub, font, data],
        tags={"TRANSCODER_TAG": "jftranscoder-v1|codec=hevc|br=6000|abcd1234abcd1234"},
    )
    doc["chapters"] = [{"id": 1}, {"id": 2}]
    p = parse_probe("/x/f.mkv", doc)
    assert len(p.audio_streams) == 2
    assert p.audio_streams[0]["codec"] == "truehd"
    assert p.audio_streams[0]["language"] == "fre"
    assert len(p.subtitle_streams) == 1
    assert p.subtitle_streams[0]["forced"] is True
    assert p.attachment_count == 1
    assert p.data_stream_count == 1
    assert p.chapter_count == 2
    assert p.transcoder_tag == "jftranscoder-v1|codec=hevc|br=6000|abcd1234abcd1234"


def test_attached_pic_is_not_the_video_stream() -> None:
    cover = _video_stream(codec_name="mjpeg", disposition={"attached_pic": 1})
    real = _video_stream(index=1, codec_name="hevc")
    p = parse_probe("/x/f.mkv", _fmt([cover, real]))
    assert p.video_codec == "hevc"
    assert p.video_stream_count == 1


def test_no_video_stream() -> None:
    p = parse_probe("/x/f.mkv", _fmt([]))
    assert p.video_codec is None
    assert p.resolution_bucket is None
    assert p.hdr_format is HdrFormat.SDR
