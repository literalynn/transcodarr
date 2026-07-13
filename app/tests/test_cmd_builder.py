"""Builder FFmpeg (§5.3) — matrice codec × API × HDR/DV × conteneur.

C'est la cible principale des tests du module 2 : chaque variante de
commande est vérifiée structurellement, sans binaire FFmpeg ni GPU.
"""

from __future__ import annotations

import pytest

from transcoder.core.cmd_builder import BuildError, BuildParams, build_ffmpeg_cmd
from transcoder.core.dovi import DvCapabilities, plan_treatment
from transcoder.core.probe import MediaProbe, parse_probe
from transcoder.core.rules import ResolvedRule, compute_rule_hash
from transcoder.enums import Codec, Container, DvPolicy, GpuApi, HdrPolicy, ResolutionBucket

CAPS = DvCapabilities(libplacebo_dv=True, libplacebo=True)
NODE = "/dev/dri/renderD128"


def _rule(codec: Codec = Codec.HEVC, is_hdr: bool = False) -> ResolvedRule:
    return ResolvedRule(
        codec=codec,
        bucket=ResolutionBucket.P2160,
        is_hdr=is_hdr,
        target_kbps=12000,
        max_kbps=18000,
        bufsize_kbps=24000,
        rule_hash=compute_rule_hash(
            codec=codec,
            bucket=ResolutionBucket.P2160,
            is_hdr=is_hdr,
            target_kbps=12000,
            max_kbps=18000,
            bufsize_kbps=24000,
            container=Container.MKV,
            hdr_policy=HdrPolicy.PRESERVE,
            dv_policy=DvPolicy.CONVERT_TO_HDR10,
        ),
    )


def _probe(
    *,
    transfer: str | None = None,
    dv_profile: int | None = None,
    dv_compat: int | None = None,
    pix_fmt: str = "yuv420p",
) -> MediaProbe:
    side_data = []
    if dv_profile is not None:
        side_data.append(
            {
                "side_data_type": "DOVI configuration record",
                "dv_profile": dv_profile,
                "dv_bl_signal_compatibility_id": dv_compat or 0,
                "el_present_flag": 0,
                "rpu_present_flag": 1,
            }
        )
    stream = {
        "index": 0,
        "codec_type": "video",
        "codec_name": "hevc",
        "width": 3840,
        "height": 2160,
        "pix_fmt": pix_fmt,
        "color_transfer": transfer,
        "side_data_list": side_data,
        "disposition": {},
        "tags": {},
    }
    return parse_probe(
        "/media/Films/film.mkv",
        {"format": {"format_name": "matroska", "duration": "7200"}, "streams": [stream]},
    )


def _build(
    probe: MediaProbe,
    *,
    codec: Codec = Codec.HEVC,
    api: GpuApi = GpuApi.QSV,
    container: Container = Container.MKV,
    hdr_policy: HdrPolicy = HdrPolicy.PRESERVE,
    is_hdr_rule: bool | None = None,
    zero_copy: bool = False,
) -> list[str]:
    plan = plan_treatment(
        probe, hdr_policy=hdr_policy, dv_policy=DvPolicy.CONVERT_TO_HDR10, caps=CAPS
    )
    rule = _rule(codec, plan.effective_is_hdr if is_hdr_rule is None else is_hdr_rule)
    return build_ffmpeg_cmd(
        BuildParams(
            ffmpeg_path="/usr/lib/jellyfin-ffmpeg/ffmpeg",
            src_path=probe.path,
            dst_path="/media/Films/.transcoder_tmp/x.mkv",
            gpu_api=api,
            drm_node=NODE,
            rule=rule,
            container=container,
            plan=plan,
            src_bit_depth=probe.bit_depth,
            p5_zero_copy=zero_copy,
        )
    )


def _vf(cmd: list[str]) -> str:
    return cmd[cmd.index("-vf") + 1] if "-vf" in cmd else ""


def _assert_common_skeleton(cmd: list[str]) -> None:
    """Invariants du squelette §5.3, valables pour TOUTES les variantes."""
    assert cmd[0].endswith("ffmpeg")
    for flag in ("-hide_banner", "-nostdin", "-y"):
        assert flag in cmd
    # Une seule piste vidéo, tout le reste conservé
    i = cmd.index("-map")
    assert cmd[i : i + 6] == ["-map", "0", "-map", "-0:v", "-map", "0:v:0"]
    assert cmd[cmd.index("-c") + 1] == "copy"
    # Métadonnées, chapitres, tag anti-boucle, progression
    assert cmd[cmd.index("-map_metadata") + 1] == "0"
    assert cmd[cmd.index("-map_chapters") + 1] == "0"
    tag = cmd[cmd.index("-metadata") + 1]
    assert tag.startswith("TRANSCODER_TAG=jftranscoder-v1|")
    assert cmd[cmd.index("-progress") + 1] == "pipe:1"
    assert "-nostats" in cmd
    assert cmd[-1] == "/media/Films/.transcoder_tmp/x.mkv"


# ---------------------------------------------------------------------------
# Matrice codec × API — chemins simples (SDR)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("codec", "api", "encoder"),
    [
        (Codec.HEVC, GpuApi.QSV, "hevc_qsv"),
        (Codec.AV1, GpuApi.QSV, "av1_qsv"),
        (Codec.HEVC, GpuApi.VAAPI, "hevc_vaapi"),
        (Codec.AV1, GpuApi.VAAPI, "av1_vaapi"),
    ],
)
def test_sdr_matrix(codec: Codec, api: GpuApi, encoder: str) -> None:
    cmd = _build(_probe(), codec=codec, api=api)
    _assert_common_skeleton(cmd)
    assert cmd[cmd.index("-c:v") + 1] == encoder
    assert cmd[cmd.index("-b:v") + 1] == "12000k"
    assert cmd[cmd.index("-maxrate") + 1] == "18000k"
    assert cmd[cmd.index("-bufsize") + 1] == "24000k"
    if api is GpuApi.QSV:
        assert cmd[cmd.index("-preset") + 1] == "slow"
        assert cmd[cmd.index("-pix_fmt") + 1] == "nv12"
        assert f"qsv=hw:{NODE}" in cmd
    else:
        assert "-preset" not in cmd
        assert f"vaapi=va:{NODE}" in cmd
        assert _vf(cmd) == "format=nv12,hwupload"
    # SDR passthrough : aucune colorimétrie forcée
    assert "-color_primaries" not in cmd


def test_sdr_10bit_source_stays_10bit() -> None:
    cmd = _build(_probe(pix_fmt="yuv420p10le"))
    assert cmd[cmd.index("-pix_fmt") + 1] == "p010le"
    assert cmd[cmd.index("-profile:v") + 1] == "main10"


# ---------------------------------------------------------------------------
# HDR10 / HDR10+ / HLG (§5.3 flags)
# ---------------------------------------------------------------------------
def test_hdr10_qsv_flags() -> None:
    cmd = _build(_probe(transfer="smpte2084", pix_fmt="yuv420p10le"))
    _assert_common_skeleton(cmd)
    assert cmd[cmd.index("-pix_fmt") + 1] == "p010le"
    assert cmd[cmd.index("-profile:v") + 1] == "main10"
    assert cmd[cmd.index("-color_primaries") + 1] == "bt2020"
    assert cmd[cmd.index("-color_trc") + 1] == "smpte2084"
    assert cmd[cmd.index("-colorspace") + 1] == "bt2020nc"
    assert cmd[cmd.index("-color_range") + 1] == "tv"


def test_hdr10_vaapi_uploads_p010() -> None:
    cmd = _build(_probe(transfer="smpte2084", pix_fmt="yuv420p10le"), api=GpuApi.VAAPI)
    assert _vf(cmd) == "format=p010le,hwupload"
    assert cmd[cmd.index("-color_trc") + 1] == "smpte2084"


def test_hlg_keeps_arib_std_b67() -> None:
    cmd = _build(_probe(transfer="arib-std-b67", pix_fmt="yuv420p10le"))
    assert cmd[cmd.index("-color_trc") + 1] == "arib-std-b67"
    assert cmd[cmd.index("-colorspace") + 1] == "bt2020nc"


# ---------------------------------------------------------------------------
# Dolby Vision (§5.3.1) — par profil
# ---------------------------------------------------------------------------
def test_dv81_no_libplacebo_pq_flags() -> None:
    """8.1 : réencodage simple sans RPU, flags BT.2020/PQ, PAS de conversion."""
    cmd = _build(_probe(transfer="smpte2084", dv_profile=8, dv_compat=1, pix_fmt="yuv420p10le"))
    assert "libplacebo" not in " ".join(cmd)
    assert cmd[cmd.index("-color_trc") + 1] == "smpte2084"
    assert cmd[cmd.index("-pix_fmt") + 1] == "p010le"


def test_dv82_sdr_bt709_flags() -> None:
    """8.2 : BL SDR — flags bt709 forcés, pas de HDR."""
    cmd = _build(_probe(transfer="bt709", dv_profile=8, dv_compat=2, pix_fmt="yuv420p10le"))
    assert cmd[cmd.index("-color_primaries") + 1] == "bt709"
    assert cmd[cmd.index("-color_trc") + 1] == "bt709"
    # BL 10-bit : on ne réduit pas la profondeur
    assert cmd[cmd.index("-pix_fmt") + 1] == "p010le"


def test_dv84_hlg_flags() -> None:
    cmd = _build(_probe(transfer="arib-std-b67", dv_profile=8, dv_compat=4, pix_fmt="yuv420p10le"))
    assert cmd[cmd.index("-color_trc") + 1] == "arib-std-b67"


def test_dv7_maps_base_layer_only() -> None:
    """P7 : -map 0:v:0 ne prend que la BL, l'EL est ignorée."""
    cmd = _build(_probe(transfer="smpte2084", dv_profile=7, dv_compat=6, pix_fmt="yuv420p10le"))
    _assert_common_skeleton(cmd)  # inclut le mapping 0:v:0
    assert "libplacebo" not in " ".join(cmd)


def test_dv5_qsv_software_fallback() -> None:
    """P5 repli : libplacebo applique le RPU, sortie p010le en RAM → QSV."""
    cmd = _build(_probe(transfer="smpte2084", dv_profile=5, pix_fmt="yuv420p10le"))
    vf = _vf(cmd)
    assert "libplacebo=apply_dolbyvision=true" in vf
    assert "colorspace=bt2020nc" in vf
    assert "color_trc=smpte2084" in vf
    assert "format=p010le" in vf
    assert "hwmap" not in vf  # repli logiciel : pas de chaîne zéro-copie
    assert cmd[cmd.index("-color_trc") + 1] == "smpte2084"
    assert "-pix_fmt" not in cmd  # le format sort du filtre, pas de -pix_fmt


def test_dv5_qsv_zero_copy_chain() -> None:
    """P5 zéro-copie : chaîne Vulkan→QSV exacte du §5.3.1."""
    cmd = _build(
        _probe(transfer="smpte2084", dv_profile=5, pix_fmt="yuv420p10le"), zero_copy=True
    )
    joined = " ".join(cmd)
    assert "-init_hw_device vulkan=vk" in joined
    assert f"-init_hw_device qsv=hw:{NODE}" in joined
    assert "-filter_hw_device vk" in joined
    vf = _vf(cmd)
    assert vf.endswith(",hwmap=derive_device=qsv,format=qsv")
    assert "libplacebo=apply_dolbyvision=true" in vf


def test_dv5_vaapi_fallback_uploads() -> None:
    cmd = _build(
        _probe(transfer="smpte2084", dv_profile=5, pix_fmt="yuv420p10le"), api=GpuApi.VAAPI
    )
    vf = _vf(cmd)
    assert vf.startswith("libplacebo=apply_dolbyvision=true")
    assert vf.endswith(",hwupload")


def test_dv5_vaapi_zero_copy_derives_vulkan_from_vaapi() -> None:
    cmd = _build(
        _probe(transfer="smpte2084", dv_profile=5, pix_fmt="yuv420p10le"),
        api=GpuApi.VAAPI,
        zero_copy=True,
    )
    joined = " ".join(cmd)
    assert "vulkan=vk@va" in joined
    assert _vf(cmd).endswith(",hwmap=derive_device=vaapi")


def test_dv5_av1_target() -> None:
    cmd = _build(
        _probe(transfer="smpte2084", dv_profile=5, pix_fmt="yuv420p10le"), codec=Codec.AV1
    )
    assert cmd[cmd.index("-c:v") + 1] == "av1_qsv"
    assert "libplacebo=apply_dolbyvision=true" in _vf(cmd)


def test_skip_plan_raises() -> None:
    plan = plan_treatment(
        _probe(transfer="smpte2084", dv_profile=4),
        hdr_policy=HdrPolicy.PRESERVE,
        dv_policy=DvPolicy.CONVERT_TO_HDR10,
        caps=CAPS,
    )
    with pytest.raises(BuildError):
        build_ffmpeg_cmd(
            BuildParams(
                ffmpeg_path="ffmpeg",
                src_path="/x.mkv",
                dst_path="/y.mkv",
                gpu_api=GpuApi.QSV,
                drm_node=NODE,
                rule=_rule(),
                container=Container.MKV,
                plan=plan,
                src_bit_depth=10,
            )
        )


# ---------------------------------------------------------------------------
# Tonemap SDR (hdr_policy = tonemap_sdr)
# ---------------------------------------------------------------------------
def test_tonemap_hdr10_to_sdr() -> None:
    cmd = _build(
        _probe(transfer="smpte2084", pix_fmt="yuv420p10le"), hdr_policy=HdrPolicy.TONEMAP_SDR
    )
    vf = _vf(cmd)
    assert "tonemapping=" in vf
    assert "color_trc=bt709" in vf
    assert "format=nv12" in vf
    assert "apply_dolbyvision" not in vf  # source non DV
    assert cmd[cmd.index("-color_primaries") + 1] == "bt709"


def test_tonemap_dv_applies_rpu() -> None:
    cmd = _build(
        _probe(transfer="smpte2084", dv_profile=5, pix_fmt="yuv420p10le"),
        hdr_policy=HdrPolicy.TONEMAP_SDR,
    )
    vf = _vf(cmd)
    assert "apply_dolbyvision=true" in vf
    assert "tonemapping=" in vf


# ---------------------------------------------------------------------------
# Conteneur
# ---------------------------------------------------------------------------
def test_mp4_container_specifics() -> None:
    cmd = _build(_probe(), container=Container.MP4)
    joined = " ".join(cmd)
    assert "-map -0:t" in joined  # MP4 ne porte pas d'attachments
    assert cmd[cmd.index("-c:s") + 1] == "mov_text"
    assert "+faststart" in joined


def test_mkv_container_keeps_everything() -> None:
    cmd = _build(_probe())
    assert "-c:s" not in cmd
    assert "-0:t" not in cmd


# ---------------------------------------------------------------------------
# Matrice complète : aucune combinaison ne produit de commande incohérente
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("codec", [Codec.HEVC, Codec.AV1])
@pytest.mark.parametrize("api", [GpuApi.QSV, GpuApi.VAAPI])
@pytest.mark.parametrize(
    ("transfer", "dv_profile", "dv_compat"),
    [
        (None, None, None),  # SDR
        ("smpte2084", None, None),  # HDR10
        ("arib-std-b67", None, None),  # HLG
        ("smpte2084", 5, 0),  # DV P5
        ("smpte2084", 7, 6),  # DV P7
        ("smpte2084", 8, 1),  # DV 8.1
        ("bt709", 8, 2),  # DV 8.2
        ("arib-std-b67", 8, 4),  # DV 8.4
    ],
)
@pytest.mark.parametrize("zero_copy", [False, True])
def test_full_matrix_builds(
    codec: Codec,
    api: GpuApi,
    transfer: str | None,
    dv_profile: int | None,
    dv_compat: int | None,
    zero_copy: bool,
) -> None:
    probe = _probe(
        transfer=transfer, dv_profile=dv_profile, dv_compat=dv_compat, pix_fmt="yuv420p10le"
    )
    cmd = _build(probe, codec=codec, api=api, zero_copy=zero_copy)
    _assert_common_skeleton(cmd)
    # L'encodeur correspond toujours au couple (codec, api)
    expected = f"{codec.value}_{api.value}"
    assert cmd[cmd.index("-c:v") + 1] == expected
    # Jamais deux -vf
    assert cmd.count("-vf") <= 1
    # VAAPI a toujours un chemin d'upload vers le GPU
    if api is GpuApi.VAAPI:
        vf = _vf(cmd)
        assert "hwupload" in vf or "hwmap" in vf
