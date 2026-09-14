"""Assemblage du fichier final par mkvmerge (stratégie ``mkvmerge_mux``).

Constaté en production : certains MKV rangent leurs blocs de sous-titres
dans le désordre (205 inversions dans les PGS d'un remux Blu-ray). FFmpeg
exige des horodatages croissants par piste et DÉPLACE alors les paquets
copiés (« Non-monotonic DTS … changing to ») : jusqu'à 39 s de décalage.
mkvmerge, lui, recopie les blocs tels quels (arrondi à la microseconde).

Répartition des rôles :
- FFmpeg ne produit qu'un fichier intermédiaire : la vidéo réencodée et
  les seules pistes qu'il FAUT convertir (FLAC, SubRip…) ;
- mkvmerge assemble le fichier final : pistes de l'intermédiaire + audio,
  sous-titres, pièces jointes et chapitres de la SOURCE, bit à bit, dans
  l'ordre d'origine (vidéo en tête). Les statistiques de pistes sont
  écrites par mkvmerge lui-même.

Réservé aux sources Matroska vers une sortie MKV : la correspondance
index ffprobe → identifiant mkvmerge y est directe et vérifiée par type.
Fonctions de construction pures, testées sans binaire.
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from typing import Any

from transcoder.core.stream_plan import StreamAction, StreamDecision, StreamPlan

# Types ffprobe des pistes (hors pièces jointes) ↔ types mkvmerge -J
_TRACK_TYPES = {"video": "video", "audio": "audio", "subtitle": "subtitles"}


class AssemblyError(RuntimeError):
    """Assemblage impossible pour ce fichier (repli : muxage FFmpeg)."""


@dataclass(frozen=True, slots=True)
class AssemblyPlan:
    # Plan réduit pour FFmpeg : vidéo réencodée + pistes converties seulement
    ffmpeg_plan: StreamPlan
    source_audio_ids: tuple[int, ...]
    source_subtitle_ids: tuple[int, ...]
    # (fichier, piste) : 0 = intermédiaire FFmpeg, 1 = source
    track_order: tuple[tuple[int, int], ...]


def is_matroska(format_name: str | None) -> bool:
    return bool(format_name) and "matroska" in (format_name or "").split(",")


def source_track_ids(
    stream_plan: StreamPlan, mkvmerge_tracks: list[dict[str, Any]]
) -> dict[int, int]:
    """Index ffprobe → identifiant mkvmerge, par ordre des pistes (pur).

    ffprobe liste les pistes Matroska dans l'ordre du fichier puis les
    pièces jointes ; mkvmerge numérote les pistes dans le même ordre. Toute
    incohérence de type annule l'assemblage (jamais de piste mal attribuée)."""
    ffprobe_tracks = [
        d for d in sorted(stream_plan.decisions, key=lambda d: d.index) if d.kind in _TRACK_TYPES
    ]
    ordered = sorted(
        (t for t in mkvmerge_tracks if isinstance(t, dict) and isinstance(t.get("id"), int)),
        key=lambda t: int(t["id"]),
    )
    if len(ffprobe_tracks) != len(ordered):
        raise AssemblyError(
            f"nombre de pistes différent (ffprobe {len(ffprobe_tracks)}, mkvmerge {len(ordered)})"
        )
    mapping: dict[int, int] = {}
    for decision, track in zip(ffprobe_tracks, ordered, strict=True):
        if _TRACK_TYPES[decision.kind] != track.get("type"):
            raise AssemblyError(
                f"piste #{decision.index} : type {decision.kind} ≠ mkvmerge {track.get('type')}"
            )
        mapping[decision.index] = int(track["id"])
    return mapping


def plan_assembly(
    stream_plan: StreamPlan, mkvmerge_tracks: list[dict[str, Any]]
) -> AssemblyPlan:
    """Répartit les pistes entre FFmpeg (intermédiaire) et mkvmerge (pur)."""
    ids = source_track_ids(stream_plan, mkvmerge_tracks)
    ffmpeg_decisions: list[StreamDecision] = []
    for d in stream_plan.decisions:
        if d.action in (StreamAction.ENCODE, StreamAction.CONVERT):
            ffmpeg_decisions.append(d)
        else:
            reason = "copiée à l'identique par mkvmerge" if d.action is StreamAction.COPY else d.reason
            ffmpeg_decisions.append(
                StreamDecision(
                    index=d.index, kind=d.kind, codec=d.codec, action=StreamAction.DROP,
                    target_codec=None, language=d.language, title=d.title, reason=reason,
                )
            )
    ffmpeg_plan = StreamPlan(decisions=tuple(ffmpeg_decisions))
    intermediate_ids = {d.index: pos for pos, d in enumerate(ffmpeg_plan.mapped)}

    order: list[tuple[int, int]] = []
    audio: list[int] = []
    subtitles: list[int] = []
    for d in stream_plan.mapped:
        if d.kind not in _TRACK_TYPES:
            continue  # pièces jointes : recopiées depuis la source, hors ordre des pistes
        if d.action is StreamAction.COPY:
            order.append((1, ids[d.index]))
            (audio if d.kind == "audio" else subtitles).append(ids[d.index])
        else:
            order.append((0, intermediate_ids[d.index]))
    return AssemblyPlan(
        ffmpeg_plan=ffmpeg_plan,
        source_audio_ids=tuple(audio),
        source_subtitle_ids=tuple(subtitles),
        track_order=tuple(order),
    )


def mkvmerge_args(
    *,
    mkvmerge_path: str,
    intermediate_path: str,
    source_path: str,
    output_path: str,
    plan: AssemblyPlan,
    keep_attachments: bool = True,
) -> list[str]:
    """Commande mkvmerge d'assemblage (pur)."""
    args = [
        mkvmerge_path,
        "--output", output_path,
        # Intermédiaire FFmpeg : pistes et tags globaux (dont TRANSCODER_TAG,
        # tags de la source recopiés par -map_metadata), rien d'autre
        "--no-chapters", "--no-attachments",
        intermediate_path,
        "--no-video",
    ]
    args += (
        ["--audio-tracks", ",".join(map(str, plan.source_audio_ids))]
        if plan.source_audio_ids
        else ["--no-audio"]
    )
    args += (
        ["--subtitle-tracks", ",".join(map(str, plan.source_subtitle_ids))]
        if plan.source_subtitle_ids
        else ["--no-subtitles"]
    )
    if not keep_attachments:
        args.append("--no-attachments")
    args += [
        "--no-global-tags",  # déjà présents dans l'intermédiaire
        source_path,
        "--track-order", ",".join(f"{f}:{t}" for f, t in plan.track_order),
    ]
    return args


def intermediate_path_for(output_path: str) -> str:
    stem, _ = os.path.splitext(output_path)
    return f"{stem}.video.mkv"


async def identify_tracks(mkvmerge_path: str, path: str, timeout: float = 120.0) -> list[dict[str, Any]]:
    """Pistes d'un fichier selon ``mkvmerge -J`` (identification, sans lecture complète)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            mkvmerge_path, "-J", path,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        raise AssemblyError(f"mkvmerge introuvable : {exc}") from exc
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise AssemblyError("identification mkvmerge trop longue") from None
    try:
        data = json.loads(stdout.decode("utf-8", "replace") or "{}")
    except json.JSONDecodeError as exc:
        raise AssemblyError(f"sortie mkvmerge -J illisible : {exc}") from exc
    if proc.returncode not in (0, 1) or not data.get("container", {}).get("recognized", False):
        raise AssemblyError("fichier non reconnu par mkvmerge")
    tracks = data.get("tracks")
    return tracks if isinstance(tracks, list) else []


async def run_mkvmerge(args: list[str], timeout: float = 14400.0) -> tuple[int, str]:
    """Exécute mkvmerge : (code, sortie). 0 = OK, 1 = avertissements (OK), 2 = erreur."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            stdin=asyncio.subprocess.DEVNULL,
        )
    except OSError as exc:
        return 2, str(exc)
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return 2, "mkvmerge : délai dépassé"
    code = proc.returncode if proc.returncode is not None else 2
    return code, stdout.decode("utf-8", "replace")
