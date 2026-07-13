"""Matrice Dolby Vision §5.3.1 — implémentée telle quelle, aucune exception."""

from __future__ import annotations

from typing import Any

import pytest

from transcoder.core.dovi import (
    DvCapabilities,
    TreatmentAction,
    default_hdr10_metadata,
    l6_to_hdr10,
    plan_treatment,
)
from transcoder.core.probe import parse_probe
from transcoder.enums import DvPolicy, HdrFormat, HdrPolicy

CAPS_FULL = DvCapabilities(libplacebo_dv=True, libplacebo=True)
CAPS_NO_DV = DvCapabilities(libplacebo_dv=False, libplacebo=True)
CAPS_NONE = DvCapabilities(libplacebo_dv=False, libplacebo=False)


def _probe(
    *,
    transfer: str | None = None,
    dv_profile: int | None = None,
    dv_compat: int | None = None,
    hdr10plus: bool = False,
    mdcv: bool = False,
) -> Any:
    side_data: list[dict[str, Any]] = []
    if dv_profile is not None:
        side_data.append(
            {
                "side_data_type": "DOVI configuration record",
                "dv_profile": dv_profile,
                "dv_bl_signal_compatibility_id": dv_compat if dv_compat is not None else 0,
                "el_present_flag": 1 if dv_profile == 7 else 0,
                "rpu_present_flag": 1,
            }
        )
    if hdr10plus:
        side_data.append({"side_data_type": "HDR Dynamic Metadata SMPTE2094-40 (HDR10+)"})
    if mdcv:
        side_data.append(
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
            }
        )
    stream: dict[str, Any] = {
        "index": 0,
        "codec_type": "video",
        "codec_name": "hevc",
        "width": 3840,
        "height": 2160,
        "pix_fmt": "yuv420p10le",
        "disposition": {},
        "tags": {},
    }
    if transfer:
        stream["color_transfer"] = transfer
    if side_data:
        stream["side_data_list"] = side_data
    return parse_probe(
        "/x/f.mkv",
        {"format": {"format_name": "matroska", "duration": "600"}, "streams": [stream]},
    )


def _plan(
    probe: Any,
    hdr_policy: HdrPolicy = HdrPolicy.PRESERVE,
    dv_policy: DvPolicy = DvPolicy.CONVERT_TO_HDR10,
    caps: DvCapabilities = CAPS_FULL,
) -> Any:
    return plan_treatment(probe, hdr_policy=hdr_policy, dv_policy=dv_policy, caps=caps)


# ---------------------------------------------------------------------------
# Matrice DV (§5.3.1) — ligne par ligne
# ---------------------------------------------------------------------------
def test_dv_81_reencode_hdr10() -> None:
    p = _plan(_probe(transfer="smpte2084", dv_profile=8, dv_compat=1))
    assert p.action is TreatmentAction.HDR10
    assert p.effective_is_hdr is True
    assert p.output_hdr_format is HdrFormat.HDR10
    assert p.source_is_dv is True
    assert p.needs_rpu_applied is False


def test_dv_82_treated_as_sdr() -> None:
    p = _plan(_probe(transfer="bt709", dv_profile=8, dv_compat=2))
    assert p.action is TreatmentAction.SDR_BL
    assert p.effective_is_hdr is False  # règle de bitrate SDR (§5.3.1)
    assert p.output_hdr_format is HdrFormat.SDR


def test_dv_84_stays_hlg() -> None:
    p = _plan(_probe(transfer="arib-std-b67", dv_profile=8, dv_compat=4))
    assert p.action is TreatmentAction.HLG
    assert p.output_hdr_format is HdrFormat.HLG  # jamais de tag PQ sur du HLG


def test_dv_86_bluray_hdr10() -> None:
    p = _plan(_probe(transfer="smpte2084", dv_profile=8, dv_compat=6))
    assert p.action is TreatmentAction.HDR10


def test_dv_8_unknown_compat_skipped() -> None:
    p = _plan(_probe(transfer="smpte2084", dv_profile=8, dv_compat=3))
    assert p.action is TreatmentAction.SKIP


def test_dv_p7_bl_only_hdr10() -> None:
    p = _plan(_probe(transfer="smpte2084", dv_profile=7, dv_compat=6))
    assert p.action is TreatmentAction.HDR10
    assert p.needs_rpu_applied is False


def test_dv_p5_libplacebo_mandatory() -> None:
    p = _plan(_probe(transfer="smpte2084", dv_profile=5))
    assert p.action is TreatmentAction.P5_LIBPLACEBO
    assert p.needs_rpu_applied is True
    assert p.output_hdr_format is HdrFormat.HDR10


def test_dv_p5_without_libplacebo_is_skipped_never_best_effort() -> None:
    p = _plan(_probe(transfer="smpte2084", dv_profile=5), caps=CAPS_NO_DV)
    assert p.action is TreatmentAction.SKIP
    assert "conversion de gamut indisponible" in p.reason


def test_dv_p4_skipped_policy() -> None:
    p = _plan(_probe(transfer="smpte2084", dv_profile=4))
    assert p.action is TreatmentAction.SKIP
    assert "profil 4" in p.reason


def test_dv_unknown_profile_skipped() -> None:
    p = _plan(_probe(transfer="smpte2084", dv_profile=3))
    assert p.action is TreatmentAction.SKIP


def test_dv_policy_skip_dv_files() -> None:
    p = _plan(
        _probe(transfer="smpte2084", dv_profile=8, dv_compat=1),
        dv_policy=DvPolicy.SKIP_DV_FILES,
    )
    assert p.action is TreatmentAction.SKIP
    assert "Dolby Vision" in p.reason


# ---------------------------------------------------------------------------
# Non-DV : HDR10 / HDR10+ / HLG / SDR × politiques
# ---------------------------------------------------------------------------
def test_hdr10_preserve() -> None:
    p = _plan(_probe(transfer="smpte2084", mdcv=True))
    assert p.action is TreatmentAction.HDR10
    assert p.source_is_dv is False


def test_hdr10plus_becomes_static_hdr10() -> None:
    p = _plan(_probe(transfer="smpte2084", hdr10plus=True))
    assert p.action is TreatmentAction.HDR10
    assert p.output_hdr_format is HdrFormat.HDR10  # signalé hdr10 dans l'UI


def test_hlg_preserve() -> None:
    p = _plan(_probe(transfer="arib-std-b67"))
    assert p.action is TreatmentAction.HLG


def test_sdr_passthrough() -> None:
    p = _plan(_probe())
    assert p.action is TreatmentAction.SDR_PASSTHROUGH
    assert p.effective_is_hdr is False


def test_hdr_policy_skip_hdr_files() -> None:
    for probe in (
        _probe(transfer="smpte2084"),
        _probe(transfer="arib-std-b67"),
        _probe(transfer="smpte2084", dv_profile=5),
        _probe(transfer="smpte2084", dv_profile=8, dv_compat=1),
    ):
        p = _plan(probe, hdr_policy=HdrPolicy.SKIP_HDR_FILES)
        assert p.action is TreatmentAction.SKIP


def test_hdr_policy_skip_does_not_affect_sdr_and_dv82() -> None:
    assert (
        _plan(_probe(), hdr_policy=HdrPolicy.SKIP_HDR_FILES).action
        is TreatmentAction.SDR_PASSTHROUGH
    )
    # 8.2 est du SDR : la politique HDR ne le concerne pas
    p = _plan(
        _probe(transfer="bt709", dv_profile=8, dv_compat=2),
        hdr_policy=HdrPolicy.SKIP_HDR_FILES,
    )
    assert p.action is TreatmentAction.SDR_BL


def test_tonemap_sdr_policy() -> None:
    p = _plan(_probe(transfer="smpte2084"), hdr_policy=HdrPolicy.TONEMAP_SDR)
    assert p.action is TreatmentAction.TONEMAP_SDR
    assert p.effective_is_hdr is False  # règle SDR pour une sortie SDR
    assert p.output_hdr_format is HdrFormat.SDR


def test_tonemap_dv_p5_applies_rpu() -> None:
    p = _plan(_probe(transfer="smpte2084", dv_profile=5), hdr_policy=HdrPolicy.TONEMAP_SDR)
    assert p.action is TreatmentAction.TONEMAP_SDR
    assert p.needs_rpu_applied is True


def test_tonemap_without_libplacebo_skips() -> None:
    p = _plan(_probe(transfer="smpte2084"), hdr_policy=HdrPolicy.TONEMAP_SDR, caps=CAPS_NONE)
    assert p.action is TreatmentAction.SKIP


# ---------------------------------------------------------------------------
# Métadonnées L6 → HDR10 (§5.3.1)
# ---------------------------------------------------------------------------
def test_l6_conversion() -> None:
    meta = l6_to_hdr10(
        {
            "max_display_mastering_luminance": 4000,
            "min_display_mastering_luminance": 50,
            "max_content_light_level": 1200,
            "max_frame_average_light_level": 350,
        }
    )
    assert meta is not None
    assert meta.source == "rpu_l6"
    assert meta.mastering.max_luminance == 4000.0
    assert meta.mastering.min_luminance == 0.005
    assert meta.content_light.max_cll == 1200
    assert meta.content_light.max_fall == 350
    # Primaires : défaut sûr P3-D65
    assert abs(meta.mastering.red_x - 0.680) < 1e-9


def test_l6_zero_fields_fall_back_to_safe_defaults() -> None:
    meta = l6_to_hdr10(
        {
            "max_display_mastering_luminance": 1000,
            "min_display_mastering_luminance": 0,
            "max_content_light_level": 0,
            "max_frame_average_light_level": 0,
        }
    )
    assert meta is not None
    assert meta.mastering.min_luminance == 0.0001
    assert meta.content_light.max_cll == 1000
    assert meta.content_light.max_fall == 400


def test_l6_unusable_returns_none() -> None:
    assert l6_to_hdr10({}) is None
    assert l6_to_hdr10({"max_display_mastering_luminance": 0}) is None


def test_default_metadata_is_safe() -> None:
    meta = default_hdr10_metadata()
    assert meta.source == "default"
    assert meta.mastering.max_luminance == 1000.0
    assert meta.mastering.min_luminance == 0.0001
    assert meta.content_light.max_cll == 1000
    assert meta.content_light.max_fall == 400


# ---------------------------------------------------------------------------
# Garde-fou : tout plan SKIP a un motif exploitable dans l'UI
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("profile", "compat"),
    [(4, 0), (3, 0), (8, 3), (5, 0)],
)
def test_skip_always_has_reason(profile: int, compat: int) -> None:
    p = _plan(_probe(transfer="smpte2084", dv_profile=profile, dv_compat=compat), caps=CAPS_NONE)
    assert p.action is TreatmentAction.SKIP
    assert len(p.reason) > 10
