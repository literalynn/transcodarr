"""Bibliothèques musique : cible audio, éligibilité, pochette, commande FFmpeg.

Une bibliothèque musique a une cible unique (codec + conteneur + débit) :
pas d'échelle de bitrates ni de GPU — libopus/aac/libmp3lame tournent sur
CPU, dans des workers dédiés (core/queue.py) qui ne prennent jamais la place
d'un encodage vidéo. Les garanties restent celles de la vidéo : sortie à
côté de la source, validation bloquante (validate.validate_audio_output),
remplacement atomique avec l'original en corbeille.

Seule la première piste audio est encodée : un fichier qui en porte
plusieurs, ou une vraie piste vidéo, est écarté plutôt que mutilé.

Pochette : copiée telle quelle en MP3/M4A. Le muxer Ogg de FFmpeg 7.1 la
refuse comme flux (« Unsupported codec id ») : elle passe alors par un
commentaire METADATA_BLOCK_PICTURE (bloc FLAC en base64, comme
`opusenc --picture`), écrit via un fichier ffmetadata — un argument de ligne
de commande est limité à 128 Kio, une pochette en pèse souvent plus.

Marqueur anti-boucle (§5.7) : tag TRANSCODER_TAG, sauf en M4A où FFmpeg
n'écrit que les atomes iTunes standards (`use_metadata_tags` les remplacerait
TOUS par des clés mdta, illisibles pour la plupart des lecteurs) : il prend
alors la place de l'outil d'encodage (©too, relu par ffprobe comme
« encoder »). Conséquence assumée : en M4A, les tags non standards
(identifiants MusicBrainz…) ne sont pas conservés.

Tout est pur et testé sans binaire, sauf `extract_cover`.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import re
import struct
from dataclasses import dataclass
from typing import Any

from transcoder import TOOL_ID
from transcoder.core.cmd_builder import input_flags
from transcoder.core.probe import MediaProbe
from transcoder.core.strategy import Strategy
from transcoder.enums import AudioCodec, AudioContainer

# Extensions par défaut d'une bibliothèque musique
MUSIC_EXTENSIONS: tuple[str, ...] = (
    "flac", "mp3", "m4a", "ogg", "opus", "wav", "wma", "aac", "ape", "wv", "aiff", "aif",
)

ENCODERS: dict[AudioCodec, str] = {
    AudioCodec.OPUS: "libopus",
    AudioCodec.AAC: "aac",
    AudioCodec.MP3: "libmp3lame",
}

CONTAINERS: dict[AudioCodec, tuple[AudioContainer, ...]] = {
    AudioCodec.OPUS: (AudioContainer.OPUS, AudioContainer.OGG),
    AudioCodec.AAC: (AudioContainer.M4A,),
    AudioCodec.MP3: (AudioContainer.MP3,),
}

MIN_KBPS = 32
MAX_KBPS: dict[AudioCodec, int] = {
    AudioCodec.OPUS: 510,
    AudioCodec.AAC: 512,
    AudioCodec.MP3: 320,
}

_OGG = (AudioContainer.OPUS, AudioContainer.OGG)

COVER_MIME: dict[str, str] = {
    "mjpeg": "image/jpeg",
    "png": "image/png",
    "bmp": "image/bmp",
    "gif": "image/gif",
    "webp": "image/webp",
}


def target_error(codec: AudioCodec, container: AudioContainer, bitrate_kbps: int) -> str | None:
    """Cible invalide → message (validation API) ; None si valide."""
    if container not in CONTAINERS[codec]:
        allowed = ", ".join(c.value for c in CONTAINERS[codec])
        return f"conteneur {container.value} incompatible avec {codec.value} (possible : {allowed})"
    if not MIN_KBPS <= bitrate_kbps <= MAX_KBPS[codec]:
        return (
            f"débit {bitrate_kbps} kbps hors limites pour {codec.value} "
            f"({MIN_KBPS} à {MAX_KBPS[codec]} kbps)"
        )
    return None


@dataclass(frozen=True, slots=True)
class AudioTarget:
    """Cible d'encodage d'une bibliothèque musique, avec son hash anti-boucle."""

    codec: AudioCodec
    container: AudioContainer
    bitrate_kbps: int

    @property
    def rule_hash(self) -> str:
        canonical = f"audio|{self.codec.value}|{self.container.value}|{self.bitrate_kbps}"
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]

    @property
    def tag(self) -> str:
        """Même format que le tag vidéo (rules.build_transcoder_tag)."""
        return f"{TOOL_ID}|codec={self.codec.value}|br={self.bitrate_kbps}|{self.rule_hash}"


def skip_reason(probe: MediaProbe) -> str | None:
    """Fichier musical qu'on ne doit pas toucher (None = encodable)."""
    if probe.audio_codec is None:
        return "aucune piste audio"
    if len(probe.audio_streams) > 1:
        return f"{len(probe.audio_streams)} pistes audio : seule la première serait conservée"
    if probe.video_codec is not None:
        return "contient une piste vidéo (hors pochette) : non traité en bibliothèque musique"
    return None


def gain_hopeless(
    src_kbps: int | None, target: AudioTarget, min_gain_percent: float
) -> str | None:
    """Gain prévu insuffisant → motif ; None = encodage pertinent (ou débit inconnu).

    Un encodeur audio tient son débit cible : le gain attendu est le rapport
    des débits (MP3 128 kbps → Opus 128 kbps : rien à gagner)."""
    if not src_kbps:
        return None
    gain = (1.0 - target.bitrate_kbps / src_kbps) * 100.0
    if gain >= min_gain_percent:
        return None
    return (
        f"débit audio source {src_kbps} kbps pour une cible de {target.bitrate_kbps} kbps : "
        f"gain prévu {gain:.1f} % (seuil {min_gain_percent:.1f} %)"
    )


# ---------------------------------------------------------------------------
# Pochette
# ---------------------------------------------------------------------------
def cover_stream(raw: dict[str, Any]) -> dict[str, Any] | None:
    """Première pochette (flux image « attached_pic ») de la sonde brute."""
    for s in raw.get("streams") or []:
        if (
            isinstance(s, dict)
            and s.get("codec_type") == "video"
            and (s.get("disposition") or {}).get("attached_pic") == 1
            and s.get("codec_name") in COVER_MIME
        ):
            return s
    return None


def picture_block(data: bytes, *, mime: str, width: int, height: int) -> str:
    """Valeur METADATA_BLOCK_PICTURE : bloc FLAC PICTURE (type 3, couverture
    avant) encodé en base64."""
    mime_b = mime.encode("ascii")
    block = (
        struct.pack(">II", 3, len(mime_b))
        + mime_b
        + struct.pack(">I", 0)  # description vide
        + struct.pack(">IIIII", width, height, 24, 0, len(data))
        + data
    )
    return base64.b64encode(block).decode("ascii")


def ffmetadata(values: dict[str, str]) -> str:
    """Fichier ffmetadata (``-i meta.txt -map_metadata 1``), valeurs échappées."""

    def esc(value: str) -> str:
        return re.sub(r"([=;#\\\n])", r"\\\1", value)

    return ";FFMETADATA1\n" + "".join(f"{esc(k)}={esc(v)}\n" for k, v in values.items())


async def extract_cover(ffmpeg_path: str, path: str, stream_index: int) -> bytes | None:
    """Octets bruts de la pochette (copie du flux image, sans réencodage)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            ffmpeg_path, "-hide_banner", "-nostdin", "-v", "error",
            "-i", path, "-map", f"0:{stream_index}", "-c", "copy", "-f", "image2pipe", "-",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            stdin=asyncio.subprocess.DEVNULL,
        )
    except OSError:
        return None
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=120.0)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return None
    return stdout if proc.returncode == 0 and stdout else None


# ---------------------------------------------------------------------------
# Commande
# ---------------------------------------------------------------------------
def sample_rate(raw: dict[str, Any]) -> int | None:
    """Fréquence d'échantillonnage de la première piste audio."""
    for s in raw.get("streams") or []:
        if isinstance(s, dict) and s.get("codec_type") == "audio":
            try:
                return int(str(s.get("sample_rate")))
            except ValueError:
                return None
    return None


def build_audio_cmd(
    *,
    ffmpeg_path: str,
    src_path: str,
    dst_path: str,
    target: AudioTarget,
    strategy: Strategy,
    input_format: str | None,
    cover_index: int | None,
    metadata_path: str | None,
    src_sample_rate: int | None,
) -> list[str]:
    """Commande d'encodage d'un fichier musical (pure).

    ``cover_index`` : pochette à copier comme flux (MP3/M4A) ;
    ``metadata_path`` : fichier ffmetadata (pochette Ogg en METADATA_BLOCK_PICTURE)."""
    cmd = [
        ffmpeg_path, "-hide_banner", "-nostdin", "-y",
        *input_flags(strategy, input_format),
        "-i", src_path,
    ]
    if metadata_path is not None:
        cmd += ["-i", metadata_path]
    cmd += ["-map", "0:a:0"]
    if cover_index is not None and target.container not in _OGG:
        cmd += ["-map", f"0:{cover_index}", "-c:v", "copy", "-disposition:v:0", "attached_pic"]
    # Tags globaux (FLAC, MP3…) puis ceux de la piste (Ogg Vorbis/Opus : sans
    # ce second mapping, un MP3/M4A produit depuis un Ogg perdrait ses tags)
    cmd += ["-map_metadata", "0", "-map_metadata:g", "0:s:a:0"]
    if metadata_path is not None:
        cmd += ["-map_metadata", "1"]
    cmd += ["-c:a", ENCODERS[target.codec], "-b:a", f"{target.bitrate_kbps}k"]
    if target.codec is not AudioCodec.OPUS and src_sample_rate and src_sample_rate > 48000:
        # Hi-res 88,2/96/192 kHz : inaudible à 128 kbps, et hors MP3
        cmd += ["-ar", "48000"]
    tag_key = "encoding_tool" if target.container is AudioContainer.M4A else "TRANSCODER_TAG"
    cmd += [
        "-metadata", f"{tag_key}={target.tag}",
        "-progress", "pipe:1",
        "-nostats",
        dst_path,
    ]
    return cmd


__all__ = [
    "CONTAINERS",
    "ENCODERS",
    "MAX_KBPS",
    "MUSIC_EXTENSIONS",
    "AudioTarget",
    "build_audio_cmd",
    "cover_stream",
    "extract_cover",
    "ffmetadata",
    "gain_hopeless",
    "picture_block",
    "sample_rate",
    "skip_reason",
    "target_error",
]
