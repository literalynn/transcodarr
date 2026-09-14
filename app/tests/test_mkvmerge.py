"""Assemblage mkvmerge : répartition des pistes et commande (pur, sans binaire).

Structure calquée sur un fichier réel de production (film
VC-1, 4 audio, 4 PGS aux blocs désordonnés, 3 ASS)."""

from __future__ import annotations

from typing import Any

import pytest

from transcoder.core.mkvmerge import (
    AssemblyError,
    intermediate_path_for,
    is_matroska,
    mkvmerge_args,
    plan_assembly,
)
from transcoder.core.stream_plan import StreamAction, plan_streams
from transcoder.core.strategy import Strategy
from transcoder.enums import Container

FFPROBE_STREAMS: list[dict[str, Any]] = (
    [{"index": 0, "codec_type": "video", "codec_name": "vc1", "disposition": {}}]
    + [{"index": i, "codec_type": "audio", "codec_name": c}
       for i, c in [(1, "truehd"), (2, "dts"), (3, "ac3"), (4, "ac3")]]
    + [{"index": i, "codec_type": "subtitle", "codec_name": "hdmv_pgs_subtitle"} for i in (5, 6, 7, 8)]
    + [{"index": i, "codec_type": "subtitle", "codec_name": "ass"} for i in (9, 10, 11)]
    + [{"index": 12, "codec_type": "attachment", "codec_name": "ttf"}]
)
MKVMERGE_TRACKS: list[dict[str, Any]] = (
    [{"id": 0, "type": "video"}]
    + [{"id": i, "type": "audio"} for i in (1, 2, 3, 4)]
    + [{"id": i, "type": "subtitles"} for i in range(5, 12)]
)


def _plan(streams: list[dict[str, Any]] = FFPROBE_STREAMS):  # type: ignore[no-untyped-def]
    return plan_streams(streams, container=Container.MKV, strategy=Strategy.MKVMERGE_MUX)


def test_ffmpeg_only_encodes_video_when_everything_is_copyable() -> None:
    assembly = plan_assembly(_plan(), MKVMERGE_TRACKS)
    mapped = assembly.ffmpeg_plan.mapped
    assert [d.index for d in mapped] == [0]
    assert mapped[0].action is StreamAction.ENCODE
    assert assembly.source_audio_ids == (1, 2, 3, 4)
    assert assembly.source_subtitle_ids == (5, 6, 7, 8, 9, 10, 11)
    # Vidéo de l'intermédiaire en tête, puis les pistes de la source dans l'ordre
    assert assembly.track_order == ((0, 0),) + tuple((1, i) for i in range(1, 12))


def test_converted_tracks_come_from_intermediate_in_original_position() -> None:
    streams = [
        {"index": 0, "codec_type": "video", "codec_name": "h264", "disposition": {}},
        {"index": 1, "codec_type": "audio", "codec_name": "pcm_bluray"},
        {"index": 2, "codec_type": "audio", "codec_name": "ac3"},
        {"index": 3, "codec_type": "subtitle", "codec_name": "ass"},
    ]
    tracks = [{"id": 0, "type": "video"}, {"id": 1, "type": "audio"},
              {"id": 2, "type": "audio"}, {"id": 3, "type": "subtitles"}]
    assembly = plan_assembly(_plan(streams), tracks)
    assert [d.index for d in assembly.ffmpeg_plan.mapped] == [0, 1]  # vidéo + FLAC
    assert assembly.source_audio_ids == (2,)
    assert assembly.source_subtitle_ids == (3,)  # ASS recopié bit à bit
    assert assembly.track_order == ((0, 0), (0, 1), (1, 2), (1, 3))


def test_track_type_mismatch_aborts_assembly() -> None:
    tracks = [dict(t) for t in MKVMERGE_TRACKS]
    tracks[1]["type"] = "subtitles"
    with pytest.raises(AssemblyError):
        plan_assembly(_plan(), tracks)
    with pytest.raises(AssemblyError):
        plan_assembly(_plan(), MKVMERGE_TRACKS[:-1])


def test_mkvmerge_command() -> None:
    assembly = plan_assembly(_plan(), MKVMERGE_TRACKS)
    args = mkvmerge_args(
        mkvmerge_path="/usr/bin/mkvmerge",
        intermediate_path="/m/.transcoder_tmp/x.video.mkv",
        source_path="/m/film.mkv",
        output_path="/m/.transcoder_tmp/x.mkv",
        plan=assembly,
    )
    src = args.index("/m/film.mkv")
    inter = args.index("/m/.transcoder_tmp/x.video.mkv")
    assert args[args.index("--output") + 1] == "/m/.transcoder_tmp/x.mkv"
    assert inter < src
    # Options de l'intermédiaire : ni chapitres ni pièces jointes
    assert {"--no-chapters", "--no-attachments"} <= set(args[:inter])
    source_opts = args[inter + 1 : src]
    assert "--no-video" in source_opts and "--no-global-tags" in source_opts
    assert source_opts[source_opts.index("--audio-tracks") + 1] == "1,2,3,4"
    assert source_opts[source_opts.index("--subtitle-tracks") + 1] == "5,6,7,8,9,10,11"
    assert args[-2:] == ["--track-order", "0:0,1:1,1:2,1:3,1:4,1:5,1:6,1:7,1:8,1:9,1:10,1:11"]


def test_helpers() -> None:
    assert is_matroska("matroska,webm")
    assert not is_matroska("mov,mp4,m4a,3gp,3g2,mj2")
    assert not is_matroska(None)
    assert intermediate_path_for("/a/b/x.mkv") == "/a/b/x.video.mkv"
