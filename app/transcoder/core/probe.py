"""Sonde ffprobe : exécution + parsing pur vers la dataclass MediaProbe.

Deux appels ffprobe par fichier :
1. format/streams/chapters (métadonnées générales, tag anti-boucle §5.7) ;
2. side_data de la première frame vidéo (`-read_intervals "%+#1"`) pour
   le DOVI configuration record, MDCV/CLL et HDR10+ (§5.3.1).

Tout le parsing est pur (fonctions sur dict) et testé sans binaire ffprobe.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any

from transcoder.core.buckets import bucket_for_height
from transcoder.enums import HdrFormat, ResolutionBucket


class ProbeError(RuntimeError):
    """Échec de la sonde ffprobe (binaire absent, fichier illisible…)."""


# ---------------------------------------------------------------------------
# Métadonnées HDR10 statiques
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class MasteringDisplay:
    """MDCV — chromaticités CIE 1931 (0..1) et luminance en nits."""

    red_x: float
    red_y: float
    green_x: float
    green_y: float
    blue_x: float
    blue_y: float
    white_x: float
    white_y: float
    min_luminance: float
    max_luminance: float


@dataclass(frozen=True, slots=True)
class ContentLightLevel:
    """CLL — MaxCLL / MaxFALL en nits."""

    max_cll: int
    max_fall: int


# Valeurs par défaut sûres (§5.3.1) : mastering display P3-D65,
# luminance 0.0001–1000 nits ; MaxCLL 1000, MaxFALL 400.
DEFAULT_MASTERING_DISPLAY = MasteringDisplay(
    red_x=0.680,
    red_y=0.320,
    green_x=0.265,
    green_y=0.690,
    blue_x=0.150,
    blue_y=0.060,
    white_x=0.3127,
    white_y=0.3290,
    min_luminance=0.0001,
    max_luminance=1000.0,
)
DEFAULT_CONTENT_LIGHT = ContentLightLevel(max_cll=1000, max_fall=400)


# ---------------------------------------------------------------------------
# MediaProbe
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class MediaProbe:
    """Résultat complet de la sonde d'un fichier média."""

    path: str
    format_name: str | None
    duration_s: float | None

    # --- Piste vidéo principale (première piste vidéo) --------------------
    video_codec: str | None
    width: int | None
    height: int | None
    resolution_bucket: ResolutionBucket | None
    video_bitrate_kbps: int | None
    pix_fmt: str | None
    bit_depth: int | None
    color_primaries: str | None
    color_transfer: str | None
    color_space: str | None
    color_range: str | None

    # --- HDR / Dolby Vision (§5.3.1) ---------------------------------------
    is_hdr: bool
    hdr_format: HdrFormat
    dv_profile: int | None
    dv_bl_compat_id: int | None
    dv_has_el: bool | None
    dv_has_rpu: bool | None
    mastering_display: MasteringDisplay | None
    content_light: ContentLightLevel | None

    # --- Autres flux --------------------------------------------------------
    audio_streams: list[dict[str, Any]] = field(default_factory=list)
    subtitle_streams: list[dict[str, Any]] = field(default_factory=list)
    video_stream_count: int = 0
    chapter_count: int = 0
    attachment_count: int = 0
    data_stream_count: int = 0

    # --- Marqueur anti-boucle (§5.7) ---------------------------------------
    transcoder_tag: str | None = None

    # --- JSON ffprobe brut (persisté dans media_files.probe) ---------------
    raw: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Helpers de parsing (purs)
# ---------------------------------------------------------------------------
def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _opt_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _opt_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _fraction(value: Any) -> float | None:
    """Parse une valeur ffprobe de type '35400/50000' (ou nombre simple)."""
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        if "/" in value:
            num_s, _, den_s = value.partition("/")
            num, den = _opt_float(num_s), _opt_float(den_s)
            if num is None or den is None or den == 0:
                return None
            return num / den
        return _opt_float(value)
    return None


def _tag(tags: dict[str, Any], key: str) -> str | None:
    """Lecture de tag insensible à la casse (Matroska préserve la casse)."""
    for k, v in tags.items():
        if k.upper() == key.upper() and isinstance(v, str):
            return v
    return None


def _bit_depth_from_pix_fmt(pix_fmt: str | None, bits_per_raw_sample: Any) -> int | None:
    depth = _opt_int(bits_per_raw_sample)
    if depth:
        return depth
    if pix_fmt is None:
        return None
    if "12" in pix_fmt:
        return 12
    if "10" in pix_fmt:
        return 10
    return 8


def _first_video_stream(streams: list[Any]) -> dict[str, Any] | None:
    for s in streams:
        sd = _as_dict(s)
        if sd.get("codec_type") == "video" and sd.get("disposition", {}).get(
            "attached_pic", 0
        ) != 1:
            return sd
    return None


def _slim_audio(s: dict[str, Any]) -> dict[str, Any]:
    tags = _as_dict(s.get("tags"))
    return {
        "index": s.get("index"),
        "codec": s.get("codec_name"),
        "channels": s.get("channels"),
        "channel_layout": s.get("channel_layout"),
        "language": _tag(tags, "language"),
        "title": _tag(tags, "title"),
        "default": _as_dict(s.get("disposition")).get("default", 0) == 1,
    }


def _slim_subtitle(s: dict[str, Any]) -> dict[str, Any]:
    tags = _as_dict(s.get("tags"))
    return {
        "index": s.get("index"),
        "codec": s.get("codec_name"),
        "language": _tag(tags, "language"),
        "title": _tag(tags, "title"),
        "default": _as_dict(s.get("disposition")).get("default", 0) == 1,
        "forced": _as_dict(s.get("disposition")).get("forced", 0) == 1,
    }


def _parse_mastering_display(sd: dict[str, Any]) -> MasteringDisplay | None:
    values = {
        key: _fraction(sd.get(key))
        for key in (
            "red_x",
            "red_y",
            "green_x",
            "green_y",
            "blue_x",
            "blue_y",
            "white_point_x",
            "white_point_y",
            "min_luminance",
            "max_luminance",
        )
    }
    if any(v is None for v in values.values()):
        return None
    return MasteringDisplay(
        red_x=values["red_x"] or 0.0,
        red_y=values["red_y"] or 0.0,
        green_x=values["green_x"] or 0.0,
        green_y=values["green_y"] or 0.0,
        blue_x=values["blue_x"] or 0.0,
        blue_y=values["blue_y"] or 0.0,
        white_x=values["white_point_x"] or 0.0,
        white_y=values["white_point_y"] or 0.0,
        min_luminance=values["min_luminance"] or 0.0,
        max_luminance=values["max_luminance"] or 0.0,
    )


def _parse_content_light(sd: dict[str, Any]) -> ContentLightLevel | None:
    max_cll = _opt_int(sd.get("max_content"))
    max_fall = _opt_int(sd.get("max_average"))
    if max_cll is None or max_fall is None:
        return None
    return ContentLightLevel(max_cll=max_cll, max_fall=max_fall)


def _iter_side_data(*containers: dict[str, Any]) -> list[dict[str, Any]]:
    """Concatène les side_data_list des streams/frames fournis."""
    out: list[dict[str, Any]] = []
    for c in containers:
        for sd in _as_list(c.get("side_data_list")):
            sdd = _as_dict(sd)
            if sdd:
                out.append(sdd)
    return out


# ---------------------------------------------------------------------------
# Parsing principal
# ---------------------------------------------------------------------------
def parse_probe(
    path: str,
    format_json: dict[str, Any],
    frames_json: dict[str, Any] | None = None,
) -> MediaProbe:
    """Construit un MediaProbe depuis les JSON ffprobe (fonction pure)."""
    fmt = _as_dict(format_json.get("format"))
    streams = _as_list(format_json.get("streams"))
    chapters = _as_list(format_json.get("chapters"))
    first_frame = _as_dict(next(iter(_as_list((frames_json or {}).get("frames"))), None))

    video = _first_video_stream(streams)
    if video is None:
        video = {}

    duration_s = _opt_float(video.get("duration")) or _opt_float(fmt.get("duration"))

    height = _opt_int(video.get("height"))
    width = _opt_int(video.get("width"))
    pix_fmt_raw = video.get("pix_fmt")
    pix_fmt = pix_fmt_raw if isinstance(pix_fmt_raw, str) else None

    # Bitrate vidéo : bit_rate du stream, sinon tag BPS (Matroska)
    video_tags = _as_dict(video.get("tags"))
    video_bitrate_bps = _opt_int(video.get("bit_rate")) or _opt_int(
        _tag(video_tags, "BPS") or _tag(video_tags, "BPS-eng")
    )
    video_bitrate_kbps = video_bitrate_bps // 1000 if video_bitrate_bps else None

    # --- Side data : stream d'abord, première frame en complément ----------
    side_data = _iter_side_data(video, first_frame)

    dv_profile: int | None = None
    dv_bl_compat: int | None = None
    dv_has_el: bool | None = None
    dv_has_rpu: bool | None = None
    mastering: MasteringDisplay | None = None
    cll: ContentLightLevel | None = None
    has_hdr10plus = False

    for sd in side_data:
        sd_type = str(sd.get("side_data_type", ""))
        if "DOVI configuration record" in sd_type:
            dv_profile = _opt_int(sd.get("dv_profile"))
            dv_bl_compat = _opt_int(sd.get("dv_bl_signal_compatibility_id"))
            el = _opt_int(sd.get("el_present_flag"))
            rpu = _opt_int(sd.get("rpu_present_flag"))
            dv_has_el = bool(el) if el is not None else None
            dv_has_rpu = bool(rpu) if rpu is not None else None
        elif "Mastering display metadata" in sd_type and mastering is None:
            mastering = _parse_mastering_display(sd)
        elif "Content light level" in sd_type and cll is None:
            cll = _parse_content_light(sd)
        elif "HDR10+" in sd_type or "SMPTE2094-40" in sd_type:
            has_hdr10plus = True

    color_transfer_raw = video.get("color_transfer")
    color_transfer = color_transfer_raw if isinstance(color_transfer_raw, str) else None

    # --- Classification HDR (§5.1 + §5.3.1) ---------------------------------
    if dv_profile is not None:
        hdr_format = HdrFormat.DOLBY_VISION
    elif has_hdr10plus:
        hdr_format = HdrFormat.HDR10PLUS
    elif color_transfer == "smpte2084":
        hdr_format = HdrFormat.HDR10
    elif color_transfer == "arib-std-b67":
        hdr_format = HdrFormat.HLG
    elif mastering is not None or cll is not None:
        # Side data MDCV/CLL sans transfert PQ déclaré : flags absents mais
        # contenu masterisé HDR → considéré HDR10 (défensif, §5.1)
        hdr_format = HdrFormat.HDR10
    else:
        hdr_format = HdrFormat.SDR

    # is_hdr « source » : tout sauf SDR. Le cas DV 8.2 (BL SDR) est ramené
    # à un traitement SDR par la matrice de dovi.py, pas ici.
    is_hdr = hdr_format is not HdrFormat.SDR

    audio_streams = [
        _slim_audio(_as_dict(s)) for s in streams if _as_dict(s).get("codec_type") == "audio"
    ]
    subtitle_streams = [
        _slim_subtitle(_as_dict(s))
        for s in streams
        if _as_dict(s).get("codec_type") == "subtitle"
    ]
    video_count = sum(
        1
        for s in streams
        if _as_dict(s).get("codec_type") == "video"
        and _as_dict(_as_dict(s).get("disposition")).get("attached_pic", 0) != 1
    )
    attachment_count = sum(
        1 for s in streams if _as_dict(s).get("codec_type") == "attachment"
    )
    data_count = sum(1 for s in streams if _as_dict(s).get("codec_type") == "data")

    fmt_tags = _as_dict(fmt.get("tags"))
    codec_name_raw = video.get("codec_name")
    format_name_raw = fmt.get("format_name")
    primaries_raw = video.get("color_primaries")
    space_raw = video.get("color_space")
    range_raw = video.get("color_range")

    return MediaProbe(
        path=path,
        format_name=format_name_raw if isinstance(format_name_raw, str) else None,
        duration_s=duration_s,
        video_codec=codec_name_raw if isinstance(codec_name_raw, str) else None,
        width=width,
        height=height,
        resolution_bucket=bucket_for_height(height) if height else None,
        video_bitrate_kbps=video_bitrate_kbps,
        pix_fmt=pix_fmt,
        bit_depth=_bit_depth_from_pix_fmt(pix_fmt, video.get("bits_per_raw_sample")),
        color_primaries=primaries_raw if isinstance(primaries_raw, str) else None,
        color_transfer=color_transfer,
        color_space=space_raw if isinstance(space_raw, str) else None,
        color_range=range_raw if isinstance(range_raw, str) else None,
        is_hdr=is_hdr,
        hdr_format=hdr_format,
        dv_profile=dv_profile,
        dv_bl_compat_id=dv_bl_compat,
        dv_has_el=dv_has_el,
        dv_has_rpu=dv_has_rpu,
        mastering_display=mastering,
        content_light=cll,
        audio_streams=audio_streams,
        subtitle_streams=subtitle_streams,
        video_stream_count=video_count,
        chapter_count=len(chapters),
        attachment_count=attachment_count,
        data_stream_count=data_count,
        transcoder_tag=_tag(fmt_tags, "TRANSCODER_TAG"),
        raw=format_json,
    )


# ---------------------------------------------------------------------------
# Exécution ffprobe (async)
# ---------------------------------------------------------------------------
async def _run_ffprobe(args: list[str]) -> dict[str, Any]:
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:  # binaire absent / non exécutable
        raise ProbeError(f"ffprobe introuvable : {args[0]} ({exc})") from exc
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise ProbeError(
            f"ffprobe a échoué (code {proc.returncode}) : "
            f"{stderr.decode('utf-8', 'replace').strip()[:500]}"
        )
    try:
        parsed: Any = json.loads(stdout.decode("utf-8", "replace") or "{}")
    except json.JSONDecodeError as exc:
        raise ProbeError(f"sortie ffprobe illisible : {exc}") from exc
    return _as_dict(parsed)


async def probe_file(path: str, ffprobe_path: str) -> MediaProbe:
    """Sonde complète d'un fichier : format/streams/chapters + 1re frame vidéo."""
    format_json = await _run_ffprobe(
        [
            ffprobe_path,
            "-v",
            "error",
            # Cohérent avec la commande d'encodage : certaines pistes
            # (fansubs) demandent plus d'analyse pour être identifiées
            "-analyzeduration", "200M",
            "-probesize", "200M",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            "-show_chapters",
            path,
        ]
    )
    # Side data de la première frame (DOVI/MDCV/CLL/HDR10+). Peut échouer sur
    # un fichier corrompu : la sonde principale reste exploitable.
    frames_json: dict[str, Any] | None
    try:
        frames_json = await _run_ffprobe(
            [
                ffprobe_path,
                "-v",
                "error",
                "-print_format",
                "json",
                "-select_streams",
                "v:0",
                "-show_frames",
                "-read_intervals",
                "%+#1",
                path,
            ]
        )
    except ProbeError:
        frames_json = None
    return parse_probe(path, format_json, frames_json)


# ---------------------------------------------------------------------------
# Durée RÉELLE d'une piste (horodatages des paquets)
# ---------------------------------------------------------------------------
# La durée « conteneur » (format.duration) est trompeuse : en Matroska elle
# vient de l'en-tête (écrit par l'outil qui a muxé) ou du dernier paquet de
# N'IMPORTE quelle piste — un sous-titre de fansub affiché jusqu'à 9:59:59
# suffit à l'allonger d'une heure. En AVI, elle est estimée depuis le débit
# audio. Seuls les horodatages des paquets d'une piste font foi.
# Conteneurs aux horodatages absents ou approximatifs (DivX/XviD en AVI,
# TS/VOB, WMV…). Constaté en production sur un AVI : après un seek, le
# demuxer recalcule les horodatages depuis l'index de l'en-tête (1 485 s)
# alors que la lecture séquentielle — celle d'un lecteur et de l'encodage —
# s'arrête à 1 471,5 s. Pour eux, la mesure se fait par parcours complet.
LEGACY_TIMESTAMP_FORMATS = frozenset({"avi", "mpeg", "mpegts", "mpegvideo", "asf", "flv", "rm", "vob"})


def is_legacy_timestamp_format(format_name: str | None) -> bool:
    if not format_name:
        return False
    return any(token in LEGACY_TIMESTAMP_FORMATS for token in format_name.split(","))


def parse_packet_times(csv_output: str) -> list[tuple[float, float]]:
    """Lignes ``pts_time,dts_time,duration_time`` → [(temps, durée)] (pur)."""
    values: list[tuple[float, float]] = []
    for line in csv_output.splitlines():
        parts = line.strip().split(",")
        if len(parts) < 2:
            continue
        stamp = _opt_float(parts[0]) if parts[0] not in ("", "N/A") else None
        if stamp is None:
            stamp = _opt_float(parts[1]) if parts[1] not in ("", "N/A") else None
        if stamp is None:
            continue
        duration = _opt_float(parts[2]) if len(parts) > 2 else None
        values.append((stamp, duration or 0.0))
    return values


@dataclass(frozen=True, slots=True)
class StreamTimeline:
    start_s: float
    end_s: float

    @property
    def duration_s(self) -> float:
        return max(0.0, self.end_s - self.start_s)


async def _packet_times(
    ffprobe_path: str, path: str, selector: str, interval: str | None, timeout: float
) -> list[tuple[float, float]]:
    args = [
        ffprobe_path, "-v", "error", "-select_streams", selector,
        "-show_entries", "packet=pts_time,dts_time,duration_time",
        "-of", "csv=p=0",
    ]
    if interval:
        args += ["-read_intervals", interval]
    args.append(path)
    try:
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
        )
    except OSError:
        return []
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return []
    return parse_packet_times(stdout.decode("utf-8", "replace"))


async def probe_stream_timeline(
    path: str,
    ffprobe_path: str,
    *,
    selector: str = "v:0",
    duration_hint_s: float | None = None,
    full_scan: bool = False,
    timeout: float = 600.0,
) -> StreamTimeline | None:
    """Début/fin réels d'une piste d'après ses paquets. None si indisponible.

    Lecture des 30 premiers paquets puis de la fin du fichier (seek près de
    la durée annoncée) ; si le seek ne rend rien (durée annoncée fausse), ou
    si ``full_scan`` (conteneurs aux horodatages approximatifs, voir
    LEGACY_TIMESTAMP_FORMATS), parcours complet des paquets — démultiplexage
    seul, sans décodage.
    """
    head = await _packet_times(ffprobe_path, path, selector, "%+#30", 60.0)
    if not head:
        return None
    start = min(t for t, _ in head)
    tail: list[tuple[float, float]] = []
    if not full_scan and duration_hint_s and duration_hint_s > 180:
        tail = await _packet_times(
            ffprobe_path, path, selector, f"{duration_hint_s - 120:.3f}%", timeout
        )
    if not tail:
        tail = await _packet_times(ffprobe_path, path, selector, None, timeout)
    if not tail:
        return None
    end = max(t + d for t, d in tail)
    return StreamTimeline(start_s=start, end_s=end)
