"""Pré-vol : prédiction de gain (calibrée sur la production), accès source."""

from __future__ import annotations

import os
import time
from pathlib import Path

from transcoder.core.preflight import (
    check_source_access,
    predict_gain,
    significant_decode_errors,
)
from transcoder.core.probe import is_legacy_timestamp_format, parse_packet_times

EPISODE_S = 1440.0


def _size(video_kbps: float, other_kbps: float = 160.0, duration_s: float = EPISODE_S) -> int:
    return int((video_kbps + other_kbps) * duration_s * 125)


def test_source_below_target_is_hopeless() -> None:
    """Cas type de la prod : x265 1400 kbps → cible AV1 3000 kbps."""
    p = predict_gain(
        src_video_kbps=1400, size_bytes=_size(1400), duration_s=EPISODE_S,
        target_kbps=3000, min_gain_percent=5.0,
    )
    assert p.hopeless and p.predicted_gain_percent is not None and p.predicted_gain_percent < 0


def test_source_just_below_target_still_encoded() -> None:
    """Succès réels observés à src/cible = 0,98 (gain 8-11 %) : conservés."""
    p = predict_gain(
        src_video_kbps=2940, size_bytes=_size(2940), duration_s=EPISODE_S,
        target_kbps=3000, min_gain_percent=5.0,
    )
    assert not p.hopeless


def test_high_bitrate_source_is_encoded() -> None:
    p = predict_gain(
        src_video_kbps=12000, size_bytes=_size(12000, 640, 7200), duration_s=7200,
        target_kbps=6000, min_gain_percent=5.0,
    )
    assert not p.hopeless and (p.predicted_gain_percent or 0) > 40


def test_inflated_container_duration_does_not_invent_a_loss() -> None:
    """Cas réel : durée conteneur de 84 min pour un épisode de 24 min
    (sous-titre prolongé). La prédiction ne doit pas écarter le fichier."""
    p = predict_gain(
        src_video_kbps=6000, size_bytes=_size(6000), duration_s=EPISODE_S * 3.5,
        target_kbps=3000, min_gain_percent=5.0,
    )
    assert not p.hopeless


def test_scan_defers_estimated_bitrate() -> None:
    p = predict_gain(
        src_video_kbps=None, size_bytes=_size(900), duration_s=EPISODE_S,
        target_kbps=3000, min_gain_percent=5.0, allow_estimated=False,
    )
    assert not p.hopeless and p.predicted_gain_percent is None


def test_estimated_bitrate_is_more_conservative() -> None:
    size = _size(2300)
    measured = predict_gain(
        src_video_kbps=2300, size_bytes=size, duration_s=EPISODE_S,
        target_kbps=3000, min_gain_percent=5.0,
    )
    estimated = predict_gain(
        src_video_kbps=None, size_bytes=size, duration_s=EPISODE_S,
        target_kbps=3000, min_gain_percent=5.0,
    )
    assert estimated.bitrate_estimated
    assert (estimated.predicted_gain_percent or 0) > (measured.predicted_gain_percent or 0)


def test_unknown_duration_gives_no_opinion() -> None:
    p = predict_gain(
        src_video_kbps=1000, size_bytes=1000, duration_s=None, target_kbps=3000,
        min_gain_percent=5.0,
    )
    assert not p.hopeless and p.predicted_gain_percent is None


def test_check_source_access(tmp_path: Path) -> None:
    f = tmp_path / "a.mkv"
    f.write_bytes(b"x" * 2048)
    old = time.time() - 3600
    os.utime(f, (old, old))
    assert check_source_access(str(f))[0] == "ok"
    os.utime(f, None)  # modifié à l'instant : copie en cours
    assert check_source_access(str(f))[0] == "busy"
    assert check_source_access(str(tmp_path / "absent.mkv"))[0] == "error"
    empty = tmp_path / "vide.mkv"
    empty.write_bytes(b"")
    assert check_source_access(str(empty))[0] == "error"


def test_significant_decode_errors_filters_noise() -> None:
    stderr = (
        "[null @ 0x1] Application provided invalid, non monotonically increasing dts to muxer\n"
        "    Last message repeated 3 times\n"
        "[hevc @ 0x2] Could not find ref with POC 12\n"
    )
    assert significant_decode_errors(stderr) == ["[hevc @ 0x2] Could not find ref with POC 12"]


def test_parse_packet_times() -> None:
    out = "0.000000,0.000000,0.041667\nN/A,0.041667,0.041667\n,,\n119.958,119.916,0.041\n"
    values = parse_packet_times(out)
    assert values[0] == (0.0, 0.041667)
    assert values[1] == (0.041667, 0.041667)  # pts absent : dts utilisé
    assert max(t + d for t, d in values) == 119.999


def test_seek_artifacts_ignored_only_after_seek() -> None:
    stderr = "[hevc @ 0x3] Could not find ref with POC 41\n"
    assert significant_decode_errors(stderr, after_seek=True) == []
    assert significant_decode_errors(stderr) != []


def test_audio_and_subtitle_component_errors_are_ignored() -> None:
    """Sortie RÉELLE (prod, jellyfin-ffmpeg 7.1) d'un décodage `-map 0:v:0` :
    l'analyse des flux décode aussi la piste MP3 abîmée de la source."""
    stderr = (
        "[mp3float @ 0x55fbea4d5540] Header missing\n"
        "    Last message repeated 1 times\n"
        "[ac3 @ 0x556a12910c00] expacc 125 is out-of-range\n"
        "[aist#0:1/truehd @ 0x5580330a0280] [dec:truehd @ 0x5580344df180] Error submitting packet\n"
        "[pcm_bluray @ 0x1] invalid header\n"
        "[null @ 0x5636ca26d8c0] Application provided invalid timestamps\n"
    )
    assert significant_decode_errors(stderr) == []


def test_video_component_errors_are_kept() -> None:
    stderr = (
        "[hevc @ 0x55a0] Invalid NAL unit size (0 > 1234).\n"
        "[vist#0:0/av1 @ 0x55b0] [dec:libdav1d @ 0x55c0] Decoding error: Invalid data\n"
        "[in#0/matroska @ 0x55d0] Error during demuxing: I/O error\n"
        "Error while decoding stream #0:0: Invalid data found when processing input\n"
    )
    assert len(significant_decode_errors(stderr)) == 4


def test_legacy_timestamp_formats_use_full_scan() -> None:
    assert is_legacy_timestamp_format("avi")
    assert is_legacy_timestamp_format("mpegts")
    assert not is_legacy_timestamp_format("matroska,webm")
    assert not is_legacy_timestamp_format("mov,mp4,m4a,3gp,3g2,mj2")
    assert not is_legacy_timestamp_format(None)
