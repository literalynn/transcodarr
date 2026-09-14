"""Pré-vol : état de la source et pertinence de l'encodage, AVANT le GPU.

Trois questions posées à chaque fichier, avant de dépenser du temps GPU :

1. **La source est-elle saine et stable ?** lisible, pas en cours d'écriture
   (copie *arr en cours), décodable sur quelques segments — une source qui
   crache des erreurs de décodage part directement en stratégie tolérante ;
2. **Les pistes sont-elles écrivables ?** (plan de pistes, stream_plan.py) ;
3. **Le gain est-il atteignable ?** Mesuré sur la production : parmi
   ~700 jobs échantillonnés, 293/297 « gain insuffisant » avaient un
   bitrate vidéo source déjà sous la cible, contre 2/392 succès. Encoder
   pour découvrir qu'on ne gagne rien coûtait des centaines d'heures GPU.

La prédiction est volontairement OPTIMISTE sur le gain (sortie supposée
à 80 % de la cible, le 10e centile observé) : un fichier n'est écarté que
si même ce scénario favorable ne franchit pas le seuil de la bibliothèque.
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any

# Débit vidéo de sortie / cible, 10e centile mesuré (QSV VBR, prod)
OUTPUT_UNDERSHOOT_MEASURED = 0.80
# Bitrate source seulement estimé depuis taille/durée : marge plus large
OUTPUT_UNDERSHOOT_ESTIMATED = 0.60
# Fichier modifié il y a moins de N secondes : probablement en cours de copie
SOURCE_SETTLE_S = 120.0


@dataclass(frozen=True, slots=True)
class GainPrediction:
    predicted_gain_percent: float | None
    src_video_kbps: int | None
    target_kbps: int
    bitrate_estimated: bool
    hopeless: bool
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "predicted_gain_percent": self.predicted_gain_percent,
            "src_video_kbps": self.src_video_kbps,
            "target_kbps": self.target_kbps,
            "bitrate_estimated": self.bitrate_estimated,
            "hopeless": self.hopeless,
            "reason": self.reason,
        }


def predict_gain(
    *,
    src_video_kbps: int | None,
    size_bytes: int | None,
    duration_s: float | None,
    target_kbps: int,
    min_gain_percent: float,
    allow_estimated: bool = True,
) -> GainPrediction:
    """Gain de taille attendu d'un réencodage à ``target_kbps`` (pur).

    Le calcul repose sur le RAPPORT des bitrates vidéo (sortie attendue /
    source) appliqué à la part vidéo du fichier : une durée conteneur
    fausse (sous-titre qui l'allonge d'une heure) ne peut que réduire la
    part vidéo estimée, plafonnée à 100 %, jamais inventer une perte."""
    if not size_bytes or not duration_s or duration_s <= 0 or target_kbps <= 0:
        return GainPrediction(None, src_video_kbps, target_kbps, False, False, "durée ou taille inconnue")

    estimated = src_video_kbps is None or src_video_kbps <= 0
    if estimated and not allow_estimated:
        return GainPrediction(
            None, None, target_kbps, True, False,
            "bitrate vidéo inconnu : évaluation reportée au pré-vol (durée réelle mesurée)",
        )
    if estimated:
        # Même approximation que l'ordonnancement : 85 % du débit global
        src_video_kbps = max(1, int(size_bytes * 8 / duration_s / 1000 * 0.85))
    assert src_video_kbps is not None
    undershoot = OUTPUT_UNDERSHOOT_ESTIMATED if estimated else OUTPUT_UNDERSHOOT_MEASURED

    video_share = min(1.0, src_video_kbps * duration_s * 125.0 / size_bytes)
    output_ratio = target_kbps * undershoot / src_video_kbps
    gain = video_share * (1.0 - output_ratio) * 100.0
    hopeless = gain < min_gain_percent
    origin = "estimé" if estimated else "mesuré"
    reason = (
        f"bitrate vidéo source {src_video_kbps} kbps ({origin}) pour une cible de "
        f"{target_kbps} kbps : gain prévu {gain:.1f} % (seuil {min_gain_percent:.1f} %)"
    )
    return GainPrediction(round(gain, 2), src_video_kbps, target_kbps, estimated, hopeless, reason)


# ---------------------------------------------------------------------------
# Contrôles de la source
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class PreflightCheck:
    name: str
    status: str  # ok / warning / error / skipped
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "status": self.status, "detail": self.detail}


@dataclass(slots=True)
class PreflightReport:
    checks: list[PreflightCheck] = field(default_factory=list)
    gain: GainPrediction | None = None
    decode_errors: bool = False

    def add(self, name: str, status: str, detail: str) -> None:
        self.checks.append(PreflightCheck(name, status, detail))

    def as_dict(self) -> dict[str, Any]:
        return {
            "checks": [c.as_dict() for c in self.checks],
            "gain": self.gain.as_dict() if self.gain else None,
            "decode_errors": self.decode_errors,
        }


def check_source_access(path: str, *, now: float | None = None) -> tuple[str, str]:
    """(statut, détail) : lisible ? stable ? — ``error`` / ``busy`` / ``ok``."""
    try:
        st = os.stat(path)
    except OSError as exc:
        return "error", f"fichier inaccessible : {exc.strerror or exc}"
    if st.st_size <= 0:
        return "error", "fichier vide"
    try:
        with open(path, "rb") as fh:
            fh.read(1024 * 1024)
    except OSError as exc:
        return "error", f"lecture impossible : {exc.strerror or exc}"
    age = (now if now is not None else time.time()) - st.st_mtime
    if age < SOURCE_SETTLE_S:
        return "busy", f"modifié il y a {age:.0f} s : copie probablement en cours"
    return "ok", f"lisible, {st.st_size / 1024**3:.2f} Gio"


# Messages FFmpeg sans gravité lors d'un test de décodage vers null
_BENIGN_DECODE = (
    "non monotonically increasing dts",
    "Starting new cluster",
    "Last message repeated",
)

# Messages normaux d'un décodeur H.264/HEVC qui démarre au milieu d'un flux
# (seek sur un GOP ouvert : références antérieures au point d'entrée absentes)
_BENIGN_AFTER_SEEK = (
    "Could not find ref with POC",
    "Error constructing the frame RPS",
    "Skipping invalid undecodable NALU",
    "reference picture missing during reorder",
    "Missing reference picture",
    "mmco: unref short failure",
    "no frame!",
    "decode_slice_header error",
    "co located POCs unavailable",
    "first frame is no keyframe",
)

# Composants FFmpeg hors vidéo. Constaté en production : même avec
# `-map 0:v:0`, l'ouverture du fichier (analyse des flux) décode quelques
# trames de TOUTES les pistes — les trames MP3/AC3/AAC abîmées d'une piste
# audio copiée produisent alors « [mp3float @ …] Header missing » au milieu
# d'un contrôle censé ne porter que sur la vidéo.
_NON_VIDEO_COMPONENTS = frozenset(
    {
        "aac", "aac_fixed", "aac_latm", "ac3", "ac3_fixed", "eac3", "dca", "dts", "truehd",
        "mlp", "mp1", "mp1float", "mp2", "mp2float", "mp3", "mp3float", "mp3adu",
        "mp3adufloat", "flac", "opus", "libopus", "vorbis", "libvorbis", "alac", "wmav1",
        "wmav2", "wmapro", "wmalossless", "cook", "atrac3", "atrac3p", "tta", "wavpack",
        "ape", "amrnb", "amrwb", "gsm", "qdm2", "ra_144", "ra_288", "sipr", "nellymoser",
        "speex", "libspeex", "dolby_e", "s302m", "comfortnoise",
        "ass", "ssa", "subrip", "srt", "webvtt", "mov_text", "dvdsub", "dvd_subtitle",
        "pgssub", "dvbsub", "text", "microdvd", "sami", "cc_dec", "libzvbi_teletextdec",
        "xsub", "null",
    }
)
_COMPONENT_TAG = re.compile(r"^\[([^\]@]+?)\s*@\s*0x[0-9a-fA-F]+\]")


def _component(line: str) -> str | None:
    match = _COMPONENT_TAG.match(line)
    return match.group(1).strip() if match else None


def _is_non_video(component: str | None) -> bool:
    if component is None:
        return False
    if component.startswith(("aist#", "sist#", "dist#", "pcm_", "adpcm_")):
        return True
    return component in _NON_VIDEO_COMPONENTS


def significant_decode_errors(stderr: str, *, after_seek: bool = False) -> list[str]:
    """Lignes d'erreur réelles d'un décodage VIDÉO ``-v error``.

    Écarte le bruit du muxer null, les messages émis par des composants
    audio/sous-titres (pistes copiées, erreurs préexistantes dans la source)
    et, après un seek, les artefacts normaux de démarrage en milieu de GOP."""
    benign = _BENIGN_DECODE + (_BENIGN_AFTER_SEEK if after_seek else ())
    return [
        ln
        for ln in (line.strip() for line in stderr.splitlines())
        if ln and not any(b in ln for b in benign) and not _is_non_video(_component(ln))
    ]


async def quick_decode_test(
    ffmpeg_path: str,
    path: str,
    duration_s: float | None,
    *,
    segment_s: float = 3.0,
    timeout_s: float = 120.0,
) -> tuple[bool, str]:
    """Décode la vidéo de la source sur 3 courts segments (10/50/90 %).

    Retourne (sain, détail). Rapide (quelques secondes), il détecte les
    sources abîmées pour démarrer directement en stratégie tolérante.
    """
    points = [0.0]
    if duration_s and duration_s > segment_s * 4:
        points = [duration_s * r for r in (0.1, 0.5, 0.9)]
    errors: list[str] = []
    for start in points:
        args = [
            ffmpeg_path, "-hide_banner", "-nostdin", "-v", "error",
            "-ss", f"{start:.2f}", "-t", f"{segment_s:.1f}",
            "-i", path, "-map", "0:v:0", "-f", "null", "-",
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
                stdin=asyncio.subprocess.DEVNULL,
            )
        except OSError as exc:
            return True, f"test de décodage impossible : {exc}"
        try:
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return True, "test de décodage trop long : ignoré"
        lines = significant_decode_errors(
            stderr.decode("utf-8", "replace"), after_seek=start > 0
        )
        if proc.returncode != 0 and not lines:
            lines = [f"décodeur en échec à {start:.0f}s (code {proc.returncode})"]
        errors += [f"@{start:.0f}s {ln}" for ln in lines[:3]]
    if errors:
        return False, f"{len(errors)} erreur(s) de décodage : " + " | ".join(errors[:4])
    return True, f"{len(points)} segment(s) décodé(s) sans erreur"
