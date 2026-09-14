"""Plan de pistes : compatibilité conteneur, conversions, exclusions."""

from __future__ import annotations

from typing import Any

from transcoder.core.stream_plan import StreamAction, plan_streams
from transcoder.core.strategy import Strategy
from transcoder.enums import Container

VIDEO = {"index": 0, "codec_type": "video", "codec_name": "hevc", "disposition": {}}


def _actions(streams: list[dict[str, Any]], **kw: Any) -> dict[int, tuple[StreamAction, str | None]]:
    plan = plan_streams([VIDEO, *streams], **kw)
    return {d.index: (d.action, d.target_codec) for d in plan.decisions}


def test_video_first_and_secondary_video_dropped() -> None:
    plan = plan_streams(
        [
            {"index": 0, "codec_type": "audio", "codec_name": "aac"},
            {"index": 1, "codec_type": "video", "codec_name": "h264", "disposition": {}},
            {"index": 2, "codec_type": "video", "codec_name": "h264", "disposition": {}},
            {"index": 3, "codec_type": "video", "codec_name": "mjpeg", "disposition": {"attached_pic": 1}},
        ],
        container=Container.MKV,
    )
    assert [d.index for d in plan.mapped] == [1, 0]
    actions = {d.index: d.action for d in plan.decisions}
    assert actions[2] is StreamAction.DROP and actions[3] is StreamAction.DROP


def test_mkv_compat_rules() -> None:
    a = _actions(
        [
            {"index": 1, "codec_type": "audio", "codec_name": "truehd"},
            {"index": 2, "codec_type": "audio", "codec_name": "pcm_bluray"},
            {"index": 3, "codec_type": "subtitle", "codec_name": "mov_text"},
            {"index": 4, "codec_type": "subtitle", "codec_name": "hdmv_pgs_subtitle"},
            {"index": 5, "codec_type": "data", "codec_name": "bin_data"},
            {"index": 6, "codec_type": "attachment", "codec_name": "ttf"},
            {"index": 7, "codec_type": "attachment", "tags": {"mimetype": "application/x-truetype-font"}},
            {"index": 8, "codec_type": "attachment"},
            {"index": 9, "codec_type": "subtitle"},
            {"index": 10, "codec_type": "subtitle", "codec_name": "dvb_teletext"},
        ],
        container=Container.MKV,
    )
    assert a[1] == (StreamAction.COPY, None)
    assert a[2] == (StreamAction.CONVERT, "flac")
    assert a[3] == (StreamAction.CONVERT, "srt")
    assert a[4] == (StreamAction.COPY, None)
    assert a[5][0] is StreamAction.DROP
    assert a[6] == (StreamAction.COPY, None)
    assert a[7] == (StreamAction.COPY, None)
    assert a[8][0] is StreamAction.DROP
    assert a[9][0] is StreamAction.DROP
    assert a[10][0] is StreamAction.DROP


def test_safe_streams_strict_allowlist() -> None:
    streams = [
        {"index": 1, "codec_type": "audio", "codec_name": "cook"},
        {"index": 2, "codec_type": "subtitle", "codec_name": "some_future_sub"},
    ]
    std = _actions(streams, container=Container.MKV)
    assert std[1] == (StreamAction.COPY, None)
    assert std[2] == (StreamAction.COPY, None)
    safe = _actions(streams, container=Container.MKV, strategy=Strategy.SAFE_STREAMS)
    assert safe[1] == (StreamAction.CONVERT, "flac")
    assert safe[2][0] is StreamAction.DROP  # codec inconnu, jamais un ASS


def test_expected_counts_follow_plan() -> None:
    plan = plan_streams(
        [
            VIDEO,
            {"index": 1, "codec_type": "audio", "codec_name": "aac"},
            {"index": 2, "codec_type": "audio"},
            {"index": 3, "codec_type": "subtitle", "codec_name": "mov_text"},
            {"index": 4, "codec_type": "data"},
        ],
        container=Container.MKV,
    )
    assert plan.expected_audio == 1
    assert plan.expected_subtitles == 1
    assert len(plan.adjustments) == 3
    assert plan.as_dict()["adjustments"] == 3


def test_mp4_rules() -> None:
    a = _actions(
        [
            {"index": 1, "codec_type": "subtitle", "codec_name": "subrip"},
            {"index": 2, "codec_type": "attachment", "codec_name": "ttf"},
            {"index": 3, "codec_type": "subtitle", "codec_name": "hdmv_pgs_subtitle"},
        ],
        container=Container.MP4,
    )
    assert a[1] == (StreamAction.CONVERT, "mov_text")
    assert a[2][0] is StreamAction.DROP
    assert a[3] == (StreamAction.CONVERT, "mov_text")  # échec explicite documenté
    safe = _actions(
        [{"index": 3, "codec_type": "subtitle", "codec_name": "hdmv_pgs_subtitle"}],
        container=Container.MP4, strategy=Strategy.SAFE_STREAMS,
    )
    assert safe[3][0] is StreamAction.DROP


def test_ass_is_never_reencoded_nor_dropped() -> None:
    """Règle utilisateur : un réencodage ASS casse le typesetting."""
    streams = [
        {"index": 1, "codec_type": "subtitle", "codec_name": "ass"},
        {"index": 2, "codec_type": "subtitle", "codec_name": "ssa"},
    ]
    for strategy in (Strategy.STANDARD, Strategy.TOLERANT, Strategy.REENCODE_SUBS,
                     Strategy.SAFE_STREAMS, Strategy.ALT_API):
        actions = _actions(streams, container=Container.MKV, strategy=strategy)
        assert actions[1] == (StreamAction.COPY, None), strategy
        assert actions[2] == (StreamAction.COPY, None), strategy


def test_reencode_subs_only_touches_lossless_formats() -> None:
    streams = [
        {"index": 1, "codec_type": "subtitle", "codec_name": "subrip"},
        {"index": 2, "codec_type": "subtitle", "codec_name": "text"},
        {"index": 3, "codec_type": "subtitle", "codec_name": "webvtt"},
        {"index": 4, "codec_type": "subtitle", "codec_name": "hdmv_pgs_subtitle"},
        {"index": 5, "codec_type": "subtitle", "codec_name": "mov_text"},
        {"index": 6, "codec_type": "subtitle", "codec_name": "some_future_sub"},
        {"index": 7, "codec_type": "subtitle", "codec_name": "dvb_teletext"},
    ]
    std = _actions(streams, container=Container.MKV)
    assert std[1] == (StreamAction.COPY, None)
    assert std[7][0] is StreamAction.DROP

    re = _actions(streams, container=Container.MKV, strategy=Strategy.REENCODE_SUBS)
    assert re[1] == (StreamAction.CONVERT, "srt")  # SubRip → SubRip : sans perte
    assert re[2] == (StreamAction.CONVERT, "srt")
    assert re[3] == (StreamAction.COPY, None)  # WebVTT : positions perdues → copie
    assert re[4] == (StreamAction.COPY, None)  # image : copie
    assert re[5] == (StreamAction.CONVERT, "srt")  # conversion obligatoire en MKV
    assert re[6] == (StreamAction.COPY, None)  # inconnu : pas de réencodage à l'aveugle
    assert re[7] == (StreamAction.CONVERT, "srt")  # seule façon de garder le télétexte

    strict = _actions(streams, container=Container.MKV, strategy=Strategy.SAFE_STREAMS)
    assert strict[1] == (StreamAction.CONVERT, "srt")
    assert strict[3] == (StreamAction.COPY, None)
    assert strict[6][0] is StreamAction.DROP  # seulement l'impossible
    assert strict[7][0] is StreamAction.DROP


def test_reencode_subs_counts_stay_complete() -> None:
    plan = plan_streams(
        [
            VIDEO,
            {"index": 1, "codec_type": "subtitle", "codec_name": "ass"},
            {"index": 2, "codec_type": "subtitle", "codec_name": "subrip"},
        ],
        container=Container.MKV,
        strategy=Strategy.REENCODE_SUBS,
    )
    assert plan.expected_subtitles == 2
