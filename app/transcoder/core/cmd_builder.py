"""Builder de commande FFmpeg — fonction pure (§5.3, §5.3.1).

Aucun accès disque/GPU : (sonde, règle, GPU, plan de traitement, chemins)
→ list[str]. Toutes les variantes (QSV/VAAPI × HEVC/AV1 × SDR/HDR10/HLG/
DV P5/tonemap) sont couvertes par les tests unitaires sans encodeur réel.

Choix documentés :
- décodage logiciel + encodage GPU (squelette §5.3) : robuste quel que
  soit le codec source, au prix d'un peu de CPU ;
- P5 : deux variantes libplacebo — zéro-copie (chaîne Vulkan→QSV/VAAPI du
  §5.3.1, validée au démarrage par capabilities.py) et repli logiciel
  (libplacebo sort des frames p010le en RAM, l'encodeur les reprend) ;
- MDCV/MaxCLL : passthrough encodeur quand la source les porte (side data),
  et injection conteneur Matroska en post-encode (module replace) sinon —
  arbitrage 1 du plan validé ;
- mapping EXPLICITE piste par piste depuis le plan de pistes (stream_plan) :
  vidéo en premier, conversions (SubRip, FLAC…) et exclusions décidées en
  amont — plus jamais de `-map 0 -c copy` aveugle qui fait échouer le mux ;
- options d'entrée selon la stratégie (strategy.py) : horodatages
  régénérés pour les conteneurs anciens, décodage tolérant en repli.
"""

from __future__ import annotations

from dataclasses import dataclass

from transcoder.core.dovi import TreatmentAction, TreatmentPlan
from transcoder.core.probe import MediaProbe, is_legacy_timestamp_format
from transcoder.core.rules import ResolvedRule, build_transcoder_tag
from transcoder.core.stream_plan import StreamAction, StreamPlan, plan_streams
from transcoder.core.strategy import Strategy
from transcoder.enums import Codec, Container, GpuApi


class BuildError(ValueError):
    """Le plan de traitement ne permet pas de construire une commande."""


@dataclass(frozen=True, slots=True)
class BuildParams:
    """Entrées du builder — tout est explicite, rien n'est lu à l'extérieur."""

    ffmpeg_path: str
    src_path: str
    dst_path: str
    gpu_api: GpuApi
    drm_node: str
    rule: ResolvedRule
    container: Container
    plan: TreatmentPlan
    src_bit_depth: int | None
    # Sort de chaque piste (copie / conversion / exclusion), vidéo comprise
    stream_plan: StreamPlan
    # Chaîne zéro-copie Vulkan→QSV/VAAPI validée au démarrage (arbitrage 6).
    # False = repli logiciel (libplacebo sort en RAM, plus lent mais sûr).
    p5_zero_copy: bool = False
    strategy: Strategy = Strategy.STANDARD
    # format_name ffprobe de la source (« avi », « mpegts »…)
    input_format: str | None = None


_ENCODERS: dict[tuple[Codec, GpuApi], str] = {
    (Codec.HEVC, GpuApi.QSV): "hevc_qsv",
    (Codec.AV1, GpuApi.QSV): "av1_qsv",
    (Codec.HEVC, GpuApi.VAAPI): "hevc_vaapi",
    (Codec.AV1, GpuApi.VAAPI): "av1_vaapi",
}

# Flags colorimétrie par type de sortie (§5.3)
_COLOR_HDR10 = [
    "-color_primaries", "bt2020",
    "-color_trc", "smpte2084",
    "-colorspace", "bt2020nc",
    "-color_range", "tv",
]
_COLOR_HLG = [
    "-color_primaries", "bt2020",
    "-color_trc", "arib-std-b67",
    "-colorspace", "bt2020nc",
    "-color_range", "tv",
]
_COLOR_BT709 = [
    "-color_primaries", "bt709",
    "-color_trc", "bt709",
    "-colorspace", "bt709",
    "-color_range", "tv",
]


def _libplacebo_filter(*, apply_dv: bool, tonemap_sdr: bool) -> str:
    """Filtre libplacebo : application du RPU (P5) et/ou tonemap HDR→SDR."""
    opts: list[str] = []
    if apply_dv:
        # Nom d'option vérifié au démarrage via `ffmpeg -h filter=libplacebo`
        # (capabilities.py, arbitrage 5)
        opts.append("apply_dolbyvision=true")
    if tonemap_sdr:
        opts += [
            "tonemapping=auto",
            "colorspace=bt709",
            "color_primaries=bt709",
            "color_trc=bt709",
            "range=tv",
            "format=nv12",
        ]
    else:
        opts += [
            "colorspace=bt2020nc",
            "color_primaries=bt2020",
            "color_trc=smpte2084",
            "range=tv",
            "format=p010le",
        ]
    return "libplacebo=" + ":".join(opts)


def _pix_fmt_for(plan: TreatmentPlan, src_bit_depth: int | None) -> str:
    """p010le si sortie HDR ou source 10/12-bit (on ne réduit jamais), sinon nv12."""
    if plan.action in (TreatmentAction.HDR10, TreatmentAction.HLG, TreatmentAction.P5_LIBPLACEBO):
        return "p010le"
    if plan.action is TreatmentAction.TONEMAP_SDR:
        return "nv12"
    if src_bit_depth is not None and src_bit_depth >= 10:
        return "p010le"
    return "nv12"


def _color_flags(plan: TreatmentPlan) -> list[str]:
    if plan.action in (TreatmentAction.HDR10, TreatmentAction.P5_LIBPLACEBO):
        return list(_COLOR_HDR10)
    if plan.action is TreatmentAction.HLG:
        return list(_COLOR_HLG)
    if plan.action in (TreatmentAction.SDR_BL, TreatmentAction.TONEMAP_SDR):
        # 8.2 : BL SDR BT.709 — flags forcés (§5.3.1) ; tonemap : sortie bt709
        return list(_COLOR_BT709)
    # SDR passthrough : colorimétrie source conservée telle quelle
    return []


def _needs_libplacebo(plan: TreatmentPlan) -> bool:
    return plan.action in (TreatmentAction.P5_LIBPLACEBO, TreatmentAction.TONEMAP_SDR)


def _hw_init(params: BuildParams) -> list[str]:
    """Déclaration des devices matériels selon l'API et l'usage de libplacebo."""
    api, node = params.gpu_api, params.drm_node
    lp = _needs_libplacebo(params.plan)
    if api is GpuApi.QSV:
        if lp and params.p5_zero_copy:
            # Chaîne Vulkan→QSV du §5.3.1 : libplacebo sur device Vulkan,
            # hwmap vers QSV pour encoder sans redescendre en RAM.
            return [
                "-init_hw_device", "vulkan=vk",
                "-init_hw_device", f"qsv=hw:{node}",
                "-filter_hw_device", "vk",
            ]
        # Repli logiciel : libplacebo crée son instance Vulkan interne et
        # sort des frames p010le en RAM ; hevc_qsv/av1_qsv les remontent.
        return [
            "-init_hw_device", f"qsv=hw:{node}",
            "-filter_hw_device", "hw",
        ]
    # VAAPI
    if lp and params.p5_zero_copy:
        # Vulkan dérivé du device VAAPI → hwmap retour sans copie RAM
        return [
            "-init_hw_device", f"vaapi=va:{node}",
            "-init_hw_device", "vulkan=vk@va",
            "-filter_hw_device", "vk",
        ]
    return [
        "-init_hw_device", f"vaapi=va:{node}",
        "-filter_hw_device", "va",
    ]


def _video_filter(params: BuildParams) -> list[str]:
    """Construit -vf selon le plan. Vide = pas de filtre (QSV direct)."""
    plan = params.plan
    api = params.gpu_api
    pix = _pix_fmt_for(plan, params.src_bit_depth)

    if _needs_libplacebo(plan):
        lp = _libplacebo_filter(
            apply_dv=plan.source_is_dv,
            tonemap_sdr=plan.action is TreatmentAction.TONEMAP_SDR,
        )
        if api is GpuApi.QSV:
            if params.p5_zero_copy:
                return ["-vf", f"{lp},hwmap=derive_device=qsv,format=qsv"]
            return ["-vf", lp]
        # VAAPI : l'encodeur exige des surfaces VAAPI → hwupload en sortie
        if params.p5_zero_copy:
            return ["-vf", f"{lp},hwmap=derive_device=vaapi"]
        return ["-vf", f"{lp},hwupload"]

    if api is GpuApi.VAAPI:
        # Décodage logiciel → format cible → upload VAAPI
        return ["-vf", f"format={pix},hwupload"]

    # QSV sans filtre : l'encodeur accepte les frames système, -pix_fmt suffit
    return []


# Tags de statistiques mkvmerge recopiés par -map_metadata depuis la piste
# vidéo SOURCE : périmés après réencodage (purgés, recalculés post-encode)
_STALE_STAT_TAGS = (
    "BPS", "BPS-eng",
    "NUMBER_OF_BYTES", "NUMBER_OF_BYTES-eng",
    "NUMBER_OF_FRAMES", "NUMBER_OF_FRAMES-eng",
    "DURATION", "DURATION-eng",
    "_STATISTICS_WRITING_APP", "_STATISTICS_WRITING_DATE_UTC", "_STATISTICS_TAGS",
)


def _needs_genpts(input_format: str | None) -> bool:
    """Horodatages souvent absents ou approximatifs (DivX/XviD en AVI à
    B-frames empaquetées, TS/VOB, WMV…) : +genpts d'office."""
    return is_legacy_timestamp_format(input_format)


def _probe_flags(input_format: str | None) -> list[str]:
    """Analyse d'entrée agrandie UNIQUEMENT pour les conteneurs où les pistes
    ne s'identifient qu'en lisant des paquets (TS/VOB/AVI…).

    Constaté en production (jellyfin-ffmpeg 7.1) : sur un MKV, même 20M
    suffisent à faire recevoir au muxer un paquet de sous-titres tardif en
    premier ; FFmpeg « corrige » alors tous les suivants (Non-monotonic DTS)
    et déplace des milliers d'événements ASS. En Matroska/MP4, le codec de
    chaque piste vient de l'en-tête : l'analyse par défaut suffit."""
    if is_legacy_timestamp_format(input_format):
        return ["-analyzeduration", "200M", "-probesize", "200M"]
    return []


def input_flags(strategy: Strategy, input_format: str | None) -> list[str]:
    """Options d'entrée (avant -i) selon la stratégie et le conteneur source."""
    flags: list[str] = []
    if strategy.tolerant_input:
        # Horodatages régénérés, paquets corrompus écartés, erreurs de
        # décodage ignorées : la sortie est un flux PROPRE réencodé depuis
        # ce que le décodeur a pu reconstruire (validé ensuite comme toujours)
        flags += [
            "-fflags", "+genpts+discardcorrupt",
            "-err_detect", "ignore_err",
            "-max_error_rate", "1.0",
        ]
    elif _needs_genpts(input_format):
        flags += ["-fflags", "+genpts"]
    return flags


def stream_mapping_args(plan: StreamPlan, encoder: str) -> list[str]:
    """`-map` explicites (vidéo d'abord) + codec par piste de SORTIE."""
    mapped = plan.mapped
    if not mapped or mapped[0].action is not StreamAction.ENCODE:
        raise BuildError("aucune piste vidéo à réencoder dans la source")
    args: list[str] = []
    for decision in mapped:
        args += ["-map", f"0:{decision.index}"]
    for out_index, decision in enumerate(mapped):
        if decision.action is StreamAction.ENCODE:
            args += [f"-c:{out_index}", encoder]
        elif decision.action is StreamAction.CONVERT and decision.target_codec:
            args += [f"-c:{out_index}", decision.target_codec]
        else:
            args += [f"-c:{out_index}", "copy"]
    return args


def build_ffmpeg_cmd(params: BuildParams) -> list[str]:
    """Commande FFmpeg complète (§5.3). Lève BuildError si le plan est SKIP."""
    plan = params.plan
    if plan.action is TreatmentAction.SKIP:
        raise BuildError(f"fichier non traitable : {plan.reason}")

    encoder = _ENCODERS[(params.rule.codec, params.gpu_api)]
    pix = _pix_fmt_for(plan, params.src_bit_depth)
    vf = _video_filter(params)

    cmd: list[str] = [
        params.ffmpeg_path,
        "-hide_banner",
        "-nostdin",
        "-y",
        *_hw_init(params),
        *input_flags(params.strategy, params.input_format),
        *_probe_flags(params.input_format),
        "-i", params.src_path,
        # Une seule piste vidéo (la première = BL pour un DV P7) ; le reste
        # suit le plan de pistes : copie, conversion ou exclusion motivée
        *stream_mapping_args(params.stream_plan, encoder),
    ]

    if params.gpu_api is GpuApi.QSV:
        cmd += ["-preset:v", "slow"]

    cmd += [
        "-b:v", f"{params.rule.target_kbps}k",
        "-maxrate:v", f"{params.rule.max_kbps}k",
        "-bufsize:v", f"{params.rule.bufsize_kbps}k",
    ]

    if not vf and params.gpu_api is GpuApi.QSV:
        cmd += ["-pix_fmt", pix]
        if params.rule.codec is Codec.HEVC and pix == "p010le":
            cmd += ["-profile:v", "main10"]
    cmd += vf
    cmd += _color_flags(plan)

    cmd += ["-map_metadata", "0", "-map_chapters", "0"]

    # -map_metadata recopie les tags de statistiques mkvmerge de la piste
    # vidéo SOURCE (BPS, NUMBER_OF_BYTES…) : périmés après réencodage, ils
    # feraient afficher l'ancien bitrate. Purge (valeur vide = suppression) ;
    # les vraies statistiques sont recalculées post-encode par mkvpropedit.
    for stale_tag in _STALE_STAT_TAGS:
        cmd += ["-metadata:s:v:0", f"{stale_tag}="]

    if params.container is Container.MP4:
        cmd += ["-movflags", "+faststart"]
    if params.strategy.tolerant_input:
        cmd += ["-avoid_negative_ts", "make_zero"]

    cmd += [
        "-metadata", f"TRANSCODER_TAG={build_transcoder_tag(params.rule)}",
        "-max_muxing_queue_size", "2048",
        "-progress", "pipe:1",
        "-nostats",
        "-loglevel", "warning",
        params.dst_path,
    ]
    return cmd


def build_cmd_for(
    *,
    ffmpeg_path: str,
    probe: MediaProbe,
    rule: ResolvedRule,
    container: Container,
    plan: TreatmentPlan,
    gpu_api: GpuApi,
    drm_node: str,
    dst_path: str,
    p5_zero_copy: bool = False,
    strategy: Strategy = Strategy.STANDARD,
) -> list[str]:
    """Raccourci : assemble BuildParams depuis une sonde et construit la commande."""
    return build_ffmpeg_cmd(
        BuildParams(
            ffmpeg_path=ffmpeg_path,
            src_path=probe.path,
            dst_path=dst_path,
            gpu_api=gpu_api,
            drm_node=drm_node,
            rule=rule,
            container=container,
            plan=plan,
            src_bit_depth=probe.bit_depth,
            stream_plan=plan_streams(
                probe.raw.get("streams") or [], container=container, strategy=strategy
            ),
            p5_zero_copy=p5_zero_copy,
            strategy=strategy,
            input_format=probe.format_name,
        )
    )
