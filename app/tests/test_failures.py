"""Classification des échecs — sur des extraits RÉELS de logs de production."""

from __future__ import annotations

import pytest

from transcoder.core.failures import (
    FailureCategory,
    classify_ffmpeg_failure,
    classify_legacy,
    classify_validation_failure,
    describe_exit_code,
    key_error_lines,
)

LIBVA_NOISE = "\n".join(
    [
        "libva info: VA-API version 1.23.0",
        "libva info: Trying to open /usr/lib/jellyfin-ffmpeg/lib/dri/iHD_drv_video.so",
        "libva info: Found init function __vaDriverInit_1_23",
        "libva info: va_openDriver() returns 0",
    ]
    * 4
)

LOG_218_MOV_TEXT = LIBVA_NOISE + """
[vost#0:2 @ 0x55f63e29b340] Multiple -codec/-c/-acodec/-vcodec/-scodec/-dcodec options specified for stream 2, only the last option '-codec:v av1_qsv' will be used.
[matroska @ 0x55f63e2c9cc0] Subtitle codec 94213 is not supported.
[out#0/matroska @ 0x55f63e363ac0] Could not write header (incorrect codec parameters ?): Function not implemented
[vf#0:2 @ 0x55f63e363540] Error sending frames to consumers: Function not implemented
[vf#0:2 @ 0x55f63e363540] Task finished with error code: -38 (Function not implemented)
[out#0/matroska @ 0x55f63e363ac0] Nothing was written into output file, because at least one of its streams received no packets.
"""

LOG_234_DATA = LIBVA_NOISE + """
[matroska @ 0x55e3c2299300] Only audio, video, and subtitles are supported for Matroska.
[out#0/matroska @ 0x55e3c240ac40] Could not write header (incorrect codec parameters ?): Invalid argument
[vf#0:2 @ 0x55e3c230bdc0] Task finished with error code: -22 (Invalid argument)
"""

LOG_251_CORRUPT = LIBVA_NOISE + """
[mpeg4 @ 0x55a77ed15580] illegal MB_type
[mpeg4 @ 0x55a77ed15580] Error at MB: 882
[mpeg4 @ 0x55a77ebe5200] ac-tex damaged at 32 16
[vist#0:0/mpeg4 @ 0x55a77ebcd280] [dec:mpeg4 @ 0x55a77ecfa900] corrupt decoded frame
"""


def test_exit_218_mov_text_is_mux_subtitle() -> None:
    f = classify_ffmpeg_failure(218, LOG_218_MOV_TEXT)
    assert f.category is FailureCategory.MUX_SUBTITLE
    assert "ENOSYS" in f.message
    assert f.detail is not None
    assert "libva" not in f.detail  # bruit filtré
    assert "Subtitle codec 94213 is not supported" in f.detail


def test_exit_234_data_stream_is_mux_data() -> None:
    f = classify_ffmpeg_failure(234, LOG_234_DATA)
    assert f.category is FailureCategory.MUX_DATA
    assert "EINVAL" in f.message


def test_exit_251_corrupt_source() -> None:
    assert classify_ffmpeg_failure(251, LOG_251_CORRUPT).category is FailureCategory.SOURCE_CORRUPT


def test_pcm_bluray_is_mux_audio() -> None:
    log = "[matroska @ 0x1] No wav codec tag found for codec pcm_bluray\n"
    assert classify_ffmpeg_failure(234, log).category is FailureCategory.MUX_AUDIO


def test_signals() -> None:
    assert classify_ffmpeg_failure(-11, "").category is FailureCategory.ENCODER_CRASH
    assert classify_ffmpeg_failure(-9, "").category is FailureCategory.INTERRUPTED
    assert classify_ffmpeg_failure(255, "").category is FailureCategory.INTERRUPTED
    assert "SIGSEGV" in describe_exit_code(-11)


def test_hw_encoder_error() -> None:
    log = "[hevc_qsv @ 0x5] Error during encoding: device failed (-17)\n"
    assert classify_ffmpeg_failure(1, log).category is FailureCategory.HW_ENCODER


def test_unknown_without_message() -> None:
    f = classify_ffmpeg_failure(247, LIBVA_NOISE)
    assert f.category is FailureCategory.UNKNOWN
    assert "EBADF" in f.message


def test_key_error_lines_deduplicates() -> None:
    log = "\n".join(f"[mpeg4 @ 0x{i:x}] corrupt decoded frame" for i in range(40))
    lines = key_error_lines(log)
    assert lines is not None and lines.count("corrupt decoded frame") == 1


@pytest.mark.parametrize(
    ("check", "category"),
    [
        ("4_duration", FailureCategory.DURATION_MISMATCH),
        ("6_streams", FailureCategory.STREAM_MISMATCH),
        ("8_colorimetry", FailureCategory.COLORIMETRY),
        ("9_dv_colors", FailureCategory.DV_COLORS),
    ],
)
def test_validation_check_categories(check: str, category: FailureCategory) -> None:
    f = classify_validation_failure(check, "détail", exit_code=0, ffmpeg_log="")
    assert f.category is category


def test_integrity_crash_is_validator_crash() -> None:
    f = classify_validation_failure(
        "5_integrity", "arrêt", exit_code=0, ffmpeg_log="", check_meta={"crashed": True}
    )
    assert f.category is FailureCategory.VALIDATOR_CRASH


def test_exit_code_check_uses_log() -> None:
    f = classify_validation_failure(
        "1_exit_code", "code de sortie 218", exit_code=218, ffmpeg_log=LOG_218_MOV_TEXT
    )
    assert f.category is FailureCategory.MUX_SUBTITLE


@pytest.mark.parametrize(
    ("message", "category"),
    [
        ("4_duration : Δ=3606.94s (tolérance 7.18s)", FailureCategory.DURATION_MISMATCH),
        ("5_integrity : erreurs : ", FailureCategory.VALIDATOR_CRASH),
        (
            "5_integrity : erreurs : [ac3 @ 0x556a12910c00] expacc 125 is out-of-range",
            FailureCategory.SOURCE_CORRUPT,
        ),
        (
            "5_integrity : erreurs : [null @ 0x5636ca26d8c0] Application provided invalid, "
            "non monotonically increasing dts",
            FailureCategory.TIMESTAMPS,
        ),
        ("sonde impossible : ffprobe a échoué", FailureCategory.SOURCE_UNREADABLE),
        ("espace libre insuffisant sur /srv", FailureCategory.DISK_SPACE),
    ],
)
def test_legacy_messages(message: str, category: FailureCategory) -> None:
    assert classify_legacy(message, None, 0).category is category


def test_legacy_exit_code_reclassified_from_log_tail() -> None:
    f = classify_legacy("1_exit_code : code de sortie 234", LOG_234_DATA, 234)
    assert f.category is FailureCategory.MUX_DATA
