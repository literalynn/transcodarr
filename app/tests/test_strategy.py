"""Stratégies de repli et décision après échec."""

from __future__ import annotations

from transcoder.core.failures import FailureCategory
from transcoder.core.queue import MAX_ATTEMPTS_PER_SERIES, decide_after_failure
from transcoder.core.strategy import Strategy, next_strategy, parse_strategy


def test_strategies_are_cumulative() -> None:
    assert not Strategy.STANDARD.tolerant_input
    assert Strategy.TOLERANT.tolerant_input and not Strategy.TOLERANT.reencode_subtitles
    assert Strategy.REENCODE_SUBS.tolerant_input and Strategy.REENCODE_SUBS.reencode_subtitles
    assert not Strategy.REENCODE_SUBS.strict_streams
    assert Strategy.SAFE_STREAMS.reencode_subtitles and Strategy.SAFE_STREAMS.strict_streams
    assert Strategy.ALT_API.strict_streams and Strategy.ALT_API.alternate_api


def test_subtitle_error_reencodes_before_dropping() -> None:
    """Sous-titres en erreur : réencodage d'abord, exclusion en dernier recours."""
    assert (
        next_strategy(FailureCategory.MUX_SUBTITLE, ["standard"], alt_api_available=True)
        is Strategy.REENCODE_SUBS
    )
    assert next_strategy(
        FailureCategory.MUX_SUBTITLE, ["standard", "reencode_subs"], alt_api_available=True
    ) is Strategy.MKVMERGE_MUX
    assert next_strategy(
        FailureCategory.MUX_SUBTITLE,
        ["standard", "reencode_subs", "mkvmerge_mux"],
        alt_api_available=True,
    ) is Strategy.SAFE_STREAMS
    assert next_strategy(
        FailureCategory.MUX_SUBTITLE,
        ["standard", "reencode_subs", "mkvmerge_mux", "safe_streams"],
        alt_api_available=True,
    ) is None


def test_data_track_error_goes_to_safe_streams() -> None:
    assert (
        next_strategy(FailureCategory.MUX_DATA, ["standard"], alt_api_available=True)
        is Strategy.SAFE_STREAMS
    )


def test_corrupt_source_escalates() -> None:
    assert next_strategy(FailureCategory.SOURCE_CORRUPT, ["standard"], alt_api_available=False) is Strategy.TOLERANT
    assert next_strategy(
        FailureCategory.SOURCE_CORRUPT, ["standard", "tolerant"], alt_api_available=False
    ) is Strategy.SAFE_STREAMS


def test_hw_error_prefers_alt_api_when_available() -> None:
    assert next_strategy(FailureCategory.HW_ENCODER, ["standard"], alt_api_available=True) is Strategy.ALT_API
    assert next_strategy(FailureCategory.HW_ENCODER, ["standard"], alt_api_available=False) is Strategy.TOLERANT


def test_lower_levels_are_skipped_once_higher_tried() -> None:
    """alt_api inclut tolérant : inutile de redescendre après son échec."""
    assert next_strategy(
        FailureCategory.HW_ENCODER, ["standard", "alt_api"], alt_api_available=True
    ) is None


def test_policy_failures_have_no_fallback() -> None:
    for cat in (FailureCategory.COLORIMETRY, FailureCategory.DV_COLORS, FailureCategory.NO_RULE):
        assert next_strategy(cat, ["standard"], alt_api_available=True) is None


def test_parse_strategy() -> None:
    assert parse_strategy(None) is Strategy.STANDARD
    assert parse_strategy("tolerant") is Strategy.TOLERANT
    assert parse_strategy("bogus") is Strategy.STANDARD
    assert parse_strategy("preflight") is Strategy.STANDARD


def _decide(category: FailureCategory, **kw: object):  # type: ignore[no-untyped-def]
    args: dict[str, object] = dict(
        tried=["standard"], retry_count=0, attempt_count=1, current=Strategy.STANDARD,
        max_retries=2, auto_fallback=True, alt_api_available=True,
    )
    args.update(kw)
    return decide_after_failure(category, **args)  # type: ignore[arg-type]


def test_decision_fallback() -> None:
    d = _decide(FailureCategory.MUX_DATA)
    assert d.kind == "fallback" and d.next_strategy is Strategy.SAFE_STREAMS
    d = _decide(FailureCategory.MUX_SUBTITLE)
    assert d.kind == "fallback" and d.next_strategy is Strategy.REENCODE_SUBS


def test_decision_transient_retry_then_final() -> None:
    d = _decide(FailureCategory.SOURCE_BUSY)
    assert d.kind == "retry" and d.retry_in_s == 300.0 and d.next_strategy is Strategy.STANDARD
    assert _decide(FailureCategory.SOURCE_BUSY, retry_count=2).kind == "final"


def test_decision_final_when_nothing_left() -> None:
    assert _decide(FailureCategory.COLORIMETRY).kind == "final"
    assert _decide(FailureCategory.MUX_DATA, auto_fallback=False).kind == "final"


def test_decision_attempt_cap() -> None:
    d = _decide(FailureCategory.UNKNOWN, attempt_count=MAX_ATTEMPTS_PER_SERIES)
    assert d.kind == "final"


def test_fallback_skipped_when_command_would_be_identical() -> None:
    """Fichier dont les sous-titres sont en ASS : le repli « réencodage des
    sous-titres » ne changerait rien (ASS intouchable) → sauté."""
    from transcoder.core.stream_plan import plan_signature
    from transcoder.enums import Container

    streams = [
        {"index": 0, "codec_type": "video", "codec_name": "hevc"},
        {"index": 1, "codec_type": "audio", "codec_name": "aac"},
        {"index": 2, "codec_type": "subtitle", "codec_name": "ass"},
    ]

    def changes(candidate: Strategy) -> bool:
        done = Strategy.TOLERANT
        return not (
            candidate.tolerant_input == done.tolerant_input
            and plan_signature(streams, container=Container.MKV, strategy=candidate)
            == plan_signature(streams, container=Container.MKV, strategy=done)
        )

    # Après un essai tolérant : reencode_subs identique (ASS copié) et
    # safe_streams identique aussi (rien à exclure) → plus de repli utile
    assert next_strategy(
        FailureCategory.TIMESTAMPS, ["standard", "tolerant"],
        alt_api_available=False, changes_command=changes,
    ) is None
    srt_streams = [*streams[:2], {"index": 2, "codec_type": "subtitle", "codec_name": "subrip"}]

    def changes_srt(candidate: Strategy) -> bool:
        return plan_signature(srt_streams, container=Container.MKV, strategy=candidate) != (
            plan_signature(srt_streams, container=Container.MKV, strategy=Strategy.TOLERANT)
        )

    assert next_strategy(
        FailureCategory.TIMESTAMPS, ["standard", "tolerant"],
        alt_api_available=False, changes_command=changes_srt,
    ) is Strategy.REENCODE_SUBS


def test_subtitle_timing_goes_straight_to_mkvmerge_assembly() -> None:
    assert next_strategy(
        FailureCategory.SUBTITLE_TIMING, ["standard"], alt_api_available=True
    ) is Strategy.MKVMERGE_MUX
    assert next_strategy(
        FailureCategory.SUBTITLE_TIMING, ["standard", "mkvmerge_mux"], alt_api_available=True,
    ) is None
    assert Strategy.MKVMERGE_MUX.mkvmerge_assembly and Strategy.SAFE_STREAMS.mkvmerge_assembly
    assert not Strategy.REENCODE_SUBS.mkvmerge_assembly
