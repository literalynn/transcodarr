"""Plan de pistes : que devient chaque piste de la source dans la sortie.

Cause n°1 des échecs observés en production : des pistes que le conteneur
de sortie ne sait pas écrire, copiées aveuglément avec ``-map 0 -c copy`` :
- sous-titres ``mov_text`` des MP4 → Matroska refuse (code de sortie 218) ;
- pistes ``data`` (timecode tmcd, métadonnées) → Matroska refuse (234) ;
- audio ``pcm_bluray``/``pcm_dvd`` des m2ts/VOB → pas de tag Matroska ;
- pièces jointes sans type MIME, pistes au codec inconnu.

Le plan est décidé AVANT l'encodage, piste par piste (copie / conversion /
exclusion + motif lisible), puis traduit en mapping explicite par le
builder. Il fournit aussi l'attendu du contrôle de complétude des pistes.

Conversions retenues — jamais de perte évitable :
- sous-titres texte incompatibles → SubRip (texte conservé, style perdu) ;
- en repli ``reencode_subs``, seuls les sous-titres dont le réencodage est
  SANS PERTE sont reconstruits au lieu d'être copiés : SubRip et texte brut
  → SubRip (paquets et horodatages refaits, texte et balises conservés) ;
- **ASS/SSA ne sont JAMAIS réencodés ni exclus**, quelle que soit la
  stratégie : un réencodage casse très souvent le typesetting des fansubs.
  WebVTT (réglages de position) et sous-titres image sont copiés aussi ;
- audio PCM Blu-ray/DVD → FLAC (sans perte, plus compact) ;
- les exclusions ne concernent que ce qui est impossible à écrire.

Fonction pure, testée sans binaire.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from transcoder.core.strategy import Strategy
from transcoder.enums import Container


class StreamAction(str, Enum):
    ENCODE = "encode"
    COPY = "copy"
    CONVERT = "convert"
    DROP = "drop"


@dataclass(frozen=True, slots=True)
class StreamDecision:
    index: int
    kind: str  # video / audio / subtitle / attachment / data / unknown
    codec: str | None
    action: StreamAction
    target_codec: str | None = None
    language: str | None = None
    title: str | None = None
    reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "kind": self.kind,
            "codec": self.codec,
            "action": self.action.value,
            "target_codec": self.target_codec,
            "language": self.language,
            "title": self.title,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class StreamPlan:
    decisions: tuple[StreamDecision, ...]

    @property
    def video(self) -> StreamDecision | None:
        return next((d for d in self.decisions if d.action is StreamAction.ENCODE), None)

    @property
    def mapped(self) -> tuple[StreamDecision, ...]:
        """Pistes présentes en sortie, dans l'ordre de sortie (vidéo d'abord)."""
        video = [d for d in self.decisions if d.action is StreamAction.ENCODE]
        others = [
            d
            for d in self.decisions
            if d.action in (StreamAction.COPY, StreamAction.CONVERT)
        ]
        return tuple(video + others)

    def _count(self, kind: str) -> int:
        return sum(
            1
            for d in self.decisions
            if d.kind == kind and d.action in (StreamAction.COPY, StreamAction.CONVERT)
        )

    @property
    def expected_audio(self) -> int:
        return self._count("audio")

    @property
    def expected_subtitles(self) -> int:
        return self._count("subtitle")

    @property
    def adjustments(self) -> tuple[StreamDecision, ...]:
        """Écarts à la copie intégrale (conversions et exclusions)."""
        return tuple(
            d
            for d in self.decisions
            if d.action in (StreamAction.CONVERT, StreamAction.DROP)
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "streams": [d.as_dict() for d in self.decisions],
            "expected_audio": self.expected_audio,
            "expected_subtitles": self.expected_subtitles,
            "adjustments": len(self.adjustments),
        }


# ---------------------------------------------------------------------------
# Tables de compatibilité
# ---------------------------------------------------------------------------
# Sous-titres que le muxer Matroska sait écrire tels quels
MKV_SUBTITLES_OK: frozenset[str] = frozenset(
    {
        "subrip", "srt", "ass", "ssa", "text", "webvtt",
        "hdmv_pgs_subtitle", "dvd_subtitle", "dvb_subtitle",
        "hdmv_text_subtitle", "arib_caption",
    }
)
# Sous-titres texte que FFmpeg sait décoder → conversion en SubRip possible
TEXT_SUBTITLES_CONVERTIBLE: frozenset[str] = frozenset(
    {
        "mov_text", "eia_608", "microdvd", "subviewer", "subviewer1", "sami",
        "realtext", "pjs", "mpl2", "vplayer", "stl", "jacosub",
    }
)
# Sous-titres image (non convertibles en texte)
IMAGE_SUBTITLES: frozenset[str] = frozenset(
    {"hdmv_pgs_subtitle", "dvd_subtitle", "dvb_subtitle", "xsub", "dvb_teletext"}
)
# Sous-titres dont le réencodage (→ SubRip) ne perd aucune information
LOSSLESS_REENCODE_SUBTITLES: frozenset[str] = frozenset({"subrip", "srt", "text"})
# Famille ASS : intouchable (jamais réencodée, jamais exclue)
ASS_SUBTITLES: frozenset[str] = frozenset({"ass", "ssa"})
MP4_SUBTITLES_OK: frozenset[str] = frozenset({"mov_text"})

# Audio que Matroska ne sait pas écrire (pas de tag, pas de repli ACM)
MKV_AUDIO_NEEDS_FLAC: frozenset[str] = frozenset({"pcm_bluray", "pcm_dvd"})
# Liste blanche du mode ``safe_streams`` : tout le reste est converti en FLAC
MKV_AUDIO_SAFE: frozenset[str] = frozenset(
    {
        "aac", "ac3", "eac3", "dts", "truehd", "mlp", "mp3", "mp2", "mp1",
        "flac", "opus", "vorbis", "alac", "tta", "wavpack",
        "pcm_s16le", "pcm_s16be", "pcm_s24le", "pcm_s24be", "pcm_s32le",
        "pcm_s32be", "pcm_f32le", "pcm_f64le", "pcm_u8",
    }
)
MP4_AUDIO_SAFE: frozenset[str] = frozenset(
    {"aac", "ac3", "eac3", "mp3", "alac", "flac", "opus", "dts", "truehd"}
)
# Pièces jointes dont Matroska déduit le type MIME sans tag
MKV_ATTACHMENT_CODECS: frozenset[str] = frozenset({"ttf", "otf"})


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _tag(tags: dict[str, Any], key: str) -> str | None:
    for k, v in tags.items():
        if k.lower() == key.lower() and isinstance(v, str) and v:
            return v
    return None


def _codec(stream: dict[str, Any]) -> str | None:
    codec = stream.get("codec_name")
    if not isinstance(codec, str) or not codec or codec == "none":
        return None
    return codec


def _decide_video(disposition: dict[str, Any], video_chosen: bool) -> tuple[StreamAction, str | None, str | None]:
    if disposition.get("attached_pic", 0) == 1:
        return StreamAction.DROP, "jaquette (image attachée) non conservée", None
    if not video_chosen:
        return StreamAction.ENCODE, None, None
    return StreamAction.DROP, "piste vidéo secondaire (seule la première est réencodée)", None


def _decide_audio(
    codec: str | None, container: Container, strict: bool
) -> tuple[StreamAction, str | None, str | None]:
    if codec is None:
        return StreamAction.DROP, "codec audio non identifié : piste impossible à copier", None
    if container is Container.MKV and codec in MKV_AUDIO_NEEDS_FLAC:
        return StreamAction.CONVERT, f"{codec} non supporté par Matroska → FLAC (sans perte)", "flac"
    if strict and container is Container.MKV and codec not in MKV_AUDIO_SAFE:
        return StreamAction.CONVERT, f"{codec} hors liste sûre → FLAC (sans perte)", "flac"
    if strict and container is Container.MP4 and codec not in MP4_AUDIO_SAFE:
        return StreamAction.CONVERT, f"{codec} hors liste sûre MP4 → AAC", "aac"
    return StreamAction.COPY, None, None


def _decide_subtitle(
    codec: str | None, container: Container, strategy: Strategy
) -> tuple[StreamAction, str | None, str | None]:
    strict = strategy.strict_streams
    reencode = strategy.reencode_subtitles
    if codec is None:
        return StreamAction.DROP, "codec de sous-titres non identifié : piste impossible à copier", None
    if container is Container.MKV:
        if codec in ASS_SUBTITLES:
            # Typesetting : copie intacte, quelle que soit la stratégie
            return StreamAction.COPY, None, None
        if codec in LOSSLESS_REENCODE_SUBTITLES:
            if reencode:
                return (
                    StreamAction.CONVERT,
                    f"sous-titres {codec} reconstruits en SubRip (repli sans perte : "
                    "paquets et horodatages refaits)",
                    "srt",
                )
            return StreamAction.COPY, None, None
        if codec in MKV_SUBTITLES_OK:
            # Image, WebVTT et autres formats natifs Matroska : copie sans perte
            return StreamAction.COPY, None, None
        if codec in TEXT_SUBTITLES_CONVERTIBLE:
            # Conversion OBLIGATOIRE pour conserver la piste en Matroska
            return StreamAction.CONVERT, f"{codec} non supporté par Matroska → SubRip", "srt"
        if codec == "dvb_teletext":
            if reencode and not strict:
                return StreamAction.CONVERT, "télétexte : conversion SubRip tentée", "srt"
            return StreamAction.DROP, "sous-titres télétexte non supportés par Matroska", None
        if strict:
            return (
                StreamAction.DROP,
                f"sous-titres {codec} impossibles à écrire en Matroska (repli strict)",
                None,
            )
        return StreamAction.COPY, None, None
    # MP4
    if codec in MP4_SUBTITLES_OK:
        return StreamAction.COPY, None, None
    if codec in IMAGE_SUBTITLES:
        if strict:
            return StreamAction.DROP, f"sous-titres image {codec} impossibles en MP4", None
        # Choix documenté (DECISIONS.md) : échec explicite en MP4 plutôt
        # que perte silencieuse ; le repli safe_streams les exclut
        return StreamAction.CONVERT, "sous-titres image : conversion mov_text tentée", "mov_text"
    return StreamAction.CONVERT, f"{codec} → mov_text (MP4)", "mov_text"


def _decide_attachment(
    codec: str | None, tags: dict[str, Any], container: Container
) -> tuple[StreamAction, str | None, str | None]:
    if container is Container.MP4:
        return StreamAction.DROP, "pièces jointes impossibles en MP4", None
    if _tag(tags, "mimetype") or codec in MKV_ATTACHMENT_CODECS:
        return StreamAction.COPY, None, None
    return StreamAction.DROP, "pièce jointe sans type MIME (Matroska l'exige)", None


def plan_streams(
    streams: list[dict[str, Any]],
    *,
    container: Container,
    strategy: Strategy = Strategy.STANDARD,
) -> StreamPlan:
    """Décide le sort de chaque piste de la source (JSON ``-show_streams``)."""
    strict = strategy.strict_streams
    decisions: list[StreamDecision] = []
    video_chosen = False

    for raw in streams:
        stream = _as_dict(raw)
        index = stream.get("index")
        if not isinstance(index, int):
            continue
        kind = str(stream.get("codec_type") or "unknown")
        codec = _codec(stream)
        tags = _as_dict(stream.get("tags"))

        if kind == "video":
            action, reason, target = _decide_video(_as_dict(stream.get("disposition")), video_chosen)
            video_chosen = video_chosen or action is StreamAction.ENCODE
        elif kind == "audio":
            action, reason, target = _decide_audio(codec, container, strict)
        elif kind == "subtitle":
            action, reason, target = _decide_subtitle(codec, container, strategy)
        elif kind == "attachment":
            action, reason, target = _decide_attachment(codec, tags, container)
        elif kind == "data":
            action, reason, target = (
                StreamAction.DROP,
                "piste de données (timecode, métadonnées) non supportée par le conteneur",
                None,
            )
        else:
            action, reason, target = StreamAction.DROP, f"type de piste « {kind} » non supporté", None

        decisions.append(
            StreamDecision(
                index=index,
                kind=kind,
                codec=codec,
                action=action,
                target_codec=target,
                language=_tag(tags, "language"),
                title=_tag(tags, "title"),
                reason=reason,
            )
        )

    return StreamPlan(decisions=tuple(decisions))


def plan_signature(
    streams: list[dict[str, Any]], *, container: Container, strategy: Strategy
) -> tuple[tuple[int, str, str | None], ...]:
    """Empreinte du plan (index, action, codec cible) : deux stratégies de
    même empreinte produisent le même mapping de pistes."""
    plan = plan_streams(streams, container=container, strategy=strategy)
    return tuple((d.index, d.action.value, d.target_codec) for d in plan.decisions)


def streams_from_diagnostics(plan_dict: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Reconstitue les pistes (type, codec) depuis un plan enregistré."""
    out: list[dict[str, Any]] = []
    for s in (plan_dict or {}).get("streams") or []:
        if not isinstance(s, dict) or not isinstance(s.get("index"), int):
            continue
        out.append({"index": s["index"], "codec_type": s.get("kind"), "codec_name": s.get("codec")})
    return out
