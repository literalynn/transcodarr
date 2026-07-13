"""Matrice de traitement Dolby Vision / HDR (§5.3.1) + extraction L6.

Le planificateur `plan_treatment()` est LA source de vérité : il décide,
pour chaque fichier sondé, du traitement vidéo (passthrough SDR, HDR10,
HLG, conversion libplacebo P5, tonemap SDR) ou du refus (`skip`), en
fonction du profil DV, des politiques de la bibliothèque et des capacités
détectées au démarrage. Aucune approximation : un profil non couvert par
la matrice est refusé, jamais deviné.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any

from transcoder.core.probe import (
    DEFAULT_CONTENT_LIGHT,
    DEFAULT_MASTERING_DISPLAY,
    ContentLightLevel,
    MasteringDisplay,
    MediaProbe,
)
from transcoder.enums import DvPolicy, HdrFormat, HdrPolicy

logger = logging.getLogger(__name__)


class TreatmentAction(str, Enum):
    """Traitement vidéo décidé par la matrice (§5.3.1)."""

    SDR_PASSTHROUGH = "sdr_passthrough"  # source SDR : réencodage simple
    SDR_BL = "sdr_bl"  # DV 8.2 : BL SDR BT.709, flags forcés
    HDR10 = "hdr10"  # HDR10/HDR10+ natif ou DV 8.1/7 (RPU supprimé)
    HLG = "hlg"  # HLG natif ou DV 8.4 : flags arib-std-b67
    P5_LIBPLACEBO = "p5_libplacebo"  # DV 5 : conversion IPT→BT.2020 obligatoire
    TONEMAP_SDR = "tonemap_sdr"  # hdr_policy=tonemap_sdr : HDR → SDR bt709
    SKIP = "skip"  # skipped_policy, motif dans reason


@dataclass(frozen=True, slots=True)
class TreatmentPlan:
    """Décision complète pour un fichier : traitement + règle + sortie attendue."""

    action: TreatmentAction
    # is_hdr à utiliser pour choisir la règle de bitrate (un DV 8.2 → règle SDR)
    effective_is_hdr: bool
    # hdr_format attendu du fichier de sortie (HDR10+ → hdr10, §5.3)
    output_hdr_format: HdrFormat
    reason: str
    # La source est DV : le contrôle anti-vert/violet (§5.5.9) est obligatoire
    source_is_dv: bool = False
    # La conversion nécessite d'appliquer le RPU (libplacebo) — P5 uniquement
    needs_rpu_applied: bool = False


@dataclass(frozen=True, slots=True)
class DvCapabilities:
    """Capacités détectées au démarrage (capabilities.py, module 3)."""

    libplacebo_dv: bool  # libplacebo présent AVEC option apply_dolbyvision
    libplacebo: bool  # libplacebo présent (tonemap SDR)


def _skip(reason: str, *, is_dv: bool = False) -> TreatmentPlan:
    return TreatmentPlan(
        action=TreatmentAction.SKIP,
        effective_is_hdr=False,
        output_hdr_format=HdrFormat.SDR,
        reason=reason,
        source_is_dv=is_dv,
    )


def _plan_hdr10(reason: str, *, is_dv: bool) -> TreatmentPlan:
    return TreatmentPlan(
        action=TreatmentAction.HDR10,
        effective_is_hdr=True,
        output_hdr_format=HdrFormat.HDR10,
        reason=reason,
        source_is_dv=is_dv,
    )


def _plan_tonemap(reason: str, *, is_dv: bool, needs_rpu: bool) -> TreatmentPlan:
    return TreatmentPlan(
        action=TreatmentAction.TONEMAP_SDR,
        effective_is_hdr=False,
        output_hdr_format=HdrFormat.SDR,
        reason=reason,
        source_is_dv=is_dv,
        needs_rpu_applied=needs_rpu,
    )


def _plan_dv(probe: MediaProbe, hdr_policy: HdrPolicy, caps: DvCapabilities) -> TreatmentPlan:
    """Branche Dolby Vision de la matrice §5.3.1 — implémentée telle quelle."""
    profile = probe.dv_profile
    compat = probe.dv_bl_compat_id

    if profile == 8:
        # 8.x : la couche de base est auto-suffisante, son type dépend de compat_id
        if compat == 2:
            # BL SDR BT.709 : ce n'est pas du HDR — règle SDR, flags bt709
            return TreatmentPlan(
                action=TreatmentAction.SDR_BL,
                effective_is_hdr=False,
                output_hdr_format=HdrFormat.SDR,
                reason="DV 8.2 : couche de base SDR BT.709, traité comme SDR",
                source_is_dv=True,
            )
        if compat == 4:
            # BL HLG BT.2020
            if hdr_policy is HdrPolicy.SKIP_HDR_FILES:
                return _skip("politique : fichiers HDR ignorés (DV 8.4)", is_dv=True)
            if hdr_policy is HdrPolicy.TONEMAP_SDR:
                if not caps.libplacebo:
                    return _skip("tonemap SDR indisponible (libplacebo absent)", is_dv=True)
                return _plan_tonemap("DV 8.4 HLG → tonemap SDR", is_dv=True, needs_rpu=False)
            return TreatmentPlan(
                action=TreatmentAction.HLG,
                effective_is_hdr=True,
                output_hdr_format=HdrFormat.HLG,
                reason="DV 8.4 : couche de base HLG, sortie HLG (pas de tag PQ)",
                source_is_dv=True,
            )
        if compat in (1, 6):
            # 8.1 (et 8.6, BL HDR10 compatible Blu-ray) : BL = HDR10 valide.
            # Réencodage sans RPU = HDR10 correct, aucune conversion de gamut.
            if hdr_policy is HdrPolicy.SKIP_HDR_FILES:
                return _skip("politique : fichiers HDR ignorés (DV 8.1)", is_dv=True)
            if hdr_policy is HdrPolicy.TONEMAP_SDR:
                if not caps.libplacebo:
                    return _skip("tonemap SDR indisponible (libplacebo absent)", is_dv=True)
                return _plan_tonemap("DV 8.1 → tonemap SDR", is_dv=True, needs_rpu=False)
            return _plan_hdr10(
                f"DV 8.{compat} : BL HDR10, RPU supprimé, flags BT.2020/PQ forcés",
                is_dv=True,
            )
        return _skip(f"DV profil 8 compat_id={compat} : non couvert par la matrice", is_dv=True)

    if profile == 7:
        # Dual-layer BL+EL+RPU : la BL est un HDR10 valide. On ne mappe QUE
        # la couche de base (-map 0:v:0), l'EL est ignorée (perte FEL assumée).
        if hdr_policy is HdrPolicy.SKIP_HDR_FILES:
            return _skip("politique : fichiers HDR ignorés (DV P7)", is_dv=True)
        if hdr_policy is HdrPolicy.TONEMAP_SDR:
            if not caps.libplacebo:
                return _skip("tonemap SDR indisponible (libplacebo absent)", is_dv=True)
            return _plan_tonemap("DV P7 (BL seule) → tonemap SDR", is_dv=True, needs_rpu=False)
        return _plan_hdr10(
            "DV P7 : couche de base seule (EL ignorée), RPU supprimé, sortie HDR10",
            is_dv=True,
        )

    if profile == 5:
        # Single-layer IPT-PQ-C2 : AUCUNE couche compatible. Conversion de
        # gamut obligatoire via libplacebo (RPU appliqué). Jamais de best-effort.
        if hdr_policy is HdrPolicy.SKIP_HDR_FILES:
            return _skip("politique : fichiers HDR ignorés (DV P5)", is_dv=True)
        if not caps.libplacebo_dv:
            return _skip("DV P5 : conversion de gamut indisponible", is_dv=True)
        if hdr_policy is HdrPolicy.TONEMAP_SDR:
            return _plan_tonemap(
                "DV P5 → RPU appliqué (libplacebo) puis tonemap SDR",
                is_dv=True,
                needs_rpu=True,
            )
        return TreatmentPlan(
            action=TreatmentAction.P5_LIBPLACEBO,
            effective_is_hdr=True,
            output_hdr_format=HdrFormat.HDR10,
            reason="DV P5 : conversion IPT-PQ-C2 → BT.2020 PQ via libplacebo",
            source_is_dv=True,
            needs_rpu_applied=True,
        )

    if profile == 4:
        return _skip("DV profil 4 : couche de base non standard, non traité", is_dv=True)

    return _skip(f"DV profil {profile} : inconnu, non traité", is_dv=True)


def plan_treatment(
    probe: MediaProbe,
    *,
    hdr_policy: HdrPolicy,
    dv_policy: DvPolicy,
    caps: DvCapabilities,
) -> TreatmentPlan:
    """Décide du traitement d'un fichier sondé (matrice §5.3.1 + politiques)."""
    fmt = probe.hdr_format

    if fmt is HdrFormat.DOLBY_VISION:
        if dv_policy is DvPolicy.SKIP_DV_FILES:
            return _skip("politique : fichiers Dolby Vision ignorés", is_dv=True)
        return _plan_dv(probe, hdr_policy, caps)

    if fmt in (HdrFormat.HDR10, HdrFormat.HDR10PLUS):
        if hdr_policy is HdrPolicy.SKIP_HDR_FILES:
            return _skip("politique : fichiers HDR ignorés")
        if hdr_policy is HdrPolicy.TONEMAP_SDR:
            if not caps.libplacebo:
                return _skip("tonemap SDR indisponible (libplacebo absent)")
            return _plan_tonemap("HDR10 → tonemap SDR", is_dv=False, needs_rpu=False)
        reason = (
            "HDR10+ : métadonnées dynamiques perdues, sortie HDR10 statique (MDCV/CLL réinjectés)"
            if fmt is HdrFormat.HDR10PLUS
            else "HDR10 : flags BT.2020/PQ + MDCV/CLL réinjectés"
        )
        return _plan_hdr10(reason, is_dv=False)

    if fmt is HdrFormat.HLG:
        if hdr_policy is HdrPolicy.SKIP_HDR_FILES:
            return _skip("politique : fichiers HDR ignorés (HLG)")
        if hdr_policy is HdrPolicy.TONEMAP_SDR:
            if not caps.libplacebo:
                return _skip("tonemap SDR indisponible (libplacebo absent)")
            return _plan_tonemap("HLG → tonemap SDR", is_dv=False, needs_rpu=False)
        return TreatmentPlan(
            action=TreatmentAction.HLG,
            effective_is_hdr=True,
            output_hdr_format=HdrFormat.HLG,
            reason="HLG : flags arib-std-b67 préservés",
        )

    return TreatmentPlan(
        action=TreatmentAction.SDR_PASSTHROUGH,
        effective_is_hdr=False,
        output_hdr_format=HdrFormat.SDR,
        reason="SDR : réencodage simple, colorimétrie source conservée",
    )


# ---------------------------------------------------------------------------
# Métadonnées HDR10 de sortie (§5.3.1) — source > RPU L6 > défauts sûrs
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Hdr10Metadata:
    """MDCV + CLL à poser sur la sortie, avec provenance tracée."""

    mastering: MasteringDisplay
    content_light: ContentLightLevel
    source: str  # "source" | "rpu_l6" | "default"


def l6_to_hdr10(l6: dict[str, Any]) -> Hdr10Metadata | None:
    """Convertit un bloc L6 exporté par dovi_tool en MDCV/CLL (fonction pure).

    Le L6 ne porte que les luminances : les primaires sont fixées au
    mastering P3-D65 (défaut sûr §5.3.1). Les champs à 0 sont remplacés
    par les défauts (0 = « non renseigné » dans le RPU).
    """
    max_lum = l6.get("max_display_mastering_luminance")
    min_lum = l6.get("min_display_mastering_luminance")
    max_cll = l6.get("max_content_light_level")
    max_fall = l6.get("max_frame_average_light_level")
    if not isinstance(max_lum, int) or max_lum <= 0:
        return None
    min_nits = (min_lum / 10000.0) if isinstance(min_lum, int) and min_lum > 0 else 0.0001
    d = DEFAULT_MASTERING_DISPLAY
    return Hdr10Metadata(
        mastering=MasteringDisplay(
            red_x=d.red_x,
            red_y=d.red_y,
            green_x=d.green_x,
            green_y=d.green_y,
            blue_x=d.blue_x,
            blue_y=d.blue_y,
            white_x=d.white_x,
            white_y=d.white_y,
            min_luminance=min_nits,
            max_luminance=float(max_lum),
        ),
        content_light=ContentLightLevel(
            max_cll=max_cll if isinstance(max_cll, int) and max_cll > 0 else 1000,
            max_fall=max_fall if isinstance(max_fall, int) and max_fall > 0 else 400,
        ),
        source="rpu_l6",
    )


def default_hdr10_metadata() -> Hdr10Metadata:
    return Hdr10Metadata(
        mastering=DEFAULT_MASTERING_DISPLAY,
        content_light=DEFAULT_CONTENT_LIGHT,
        source="default",
    )


async def _extract_l6_via_dovi_tool(
    src_path: str, *, ffmpeg_path: str, dovi_tool_path: str, tmp_dir: str
) -> dict[str, Any] | None:
    """Extrait le RPU (ffmpeg → dovi_tool) puis exporte le niveau 6.

    Retourne le dict L6 ou None si un maillon échoue (binaire absent,
    flux sans RPU exploitable…). Jamais d'exception : l'appelant retombe
    sur les défauts sûrs.
    """
    import os
    import uuid

    rpu_path = os.path.join(tmp_dir, f"rpu-{uuid.uuid4().hex}.bin")
    export_path = os.path.join(tmp_dir, f"l6-{uuid.uuid4().hex}.json")
    try:
        # ffmpeg demuxe le flux HEVC annex-B → dovi_tool extract-rpu, reliés
        # par un pipe OS (pas de copie en mémoire Python)
        read_fd, write_fd = os.pipe()
        try:
            ffmpeg = await asyncio.create_subprocess_exec(
                ffmpeg_path,
                "-hide_banner",
                "-nostdin",
                "-v",
                "error",
                "-i",
                src_path,
                "-map",
                "0:v:0",
                "-c:v",
                "copy",
                "-bsf:v",
                "hevc_mp4toannexb",
                "-f",
                "hevc",
                "-",
                stdout=write_fd,
                stderr=asyncio.subprocess.DEVNULL,
            )
            dovi = await asyncio.create_subprocess_exec(
                dovi_tool_path,
                "extract-rpu",
                "-",
                "-o",
                rpu_path,
                stdin=read_fd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        finally:
            os.close(write_fd)
            os.close(read_fd)
        await dovi.wait()
        await ffmpeg.wait()
        if dovi.returncode != 0 or not os.path.exists(rpu_path):
            return None

        export = await asyncio.create_subprocess_exec(
            dovi_tool_path,
            "export",
            "-i",
            rpu_path,
            "--data",
            f"level6={export_path}",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await export.wait()
        if export.returncode != 0 or not os.path.exists(export_path):
            return None
        with open(export_path, encoding="utf-8") as fh:
            data: Any = json.load(fh)
        if isinstance(data, list):
            data = next((d for d in data if isinstance(d, dict)), None)
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        return None
    finally:
        import contextlib
        import os as _os

        for p in (rpu_path, export_path):
            with contextlib.suppress(OSError):
                _os.unlink(p)


async def resolve_output_hdr10_metadata(
    probe: MediaProbe,
    *,
    ffmpeg_path: str,
    dovi_tool_path: str,
    tmp_dir: str,
) -> Hdr10Metadata:
    """MDCV/CLL de sortie : source → RPU L6 (dovi_tool) → défauts sûrs (§5.3.1)."""
    if probe.mastering_display is not None and probe.content_light is not None:
        return Hdr10Metadata(
            mastering=probe.mastering_display,
            content_light=probe.content_light,
            source="source",
        )
    if probe.hdr_format is HdrFormat.DOLBY_VISION and probe.video_codec == "hevc":
        l6 = await _extract_l6_via_dovi_tool(
            probe.path,
            ffmpeg_path=ffmpeg_path,
            dovi_tool_path=dovi_tool_path,
            tmp_dir=tmp_dir,
        )
        if l6 is not None:
            meta = l6_to_hdr10(l6)
            if meta is not None:
                logger.info(
                    "MDCV/CLL reconstruits depuis le RPU L6 pour %s "
                    "(max=%.0f nits, MaxCLL=%d, MaxFALL=%d)",
                    probe.path,
                    meta.mastering.max_luminance,
                    meta.content_light.max_cll,
                    meta.content_light.max_fall,
                )
                return meta
    # Compléter avec les morceaux disponibles, sinon défauts sûrs — loggé
    # explicitement comme l'exige le §5.3.1.
    mastering = probe.mastering_display or DEFAULT_MASTERING_DISPLAY
    cll = probe.content_light or DEFAULT_CONTENT_LIGHT
    source = "source" if probe.mastering_display and probe.content_light else "default"
    if source == "default":
        logger.warning(
            "MDCV/CLL absents de la source %s : valeurs par défaut sûres "
            "appliquées (P3-D65, 0.0001–1000 nits, MaxCLL 1000, MaxFALL 400)",
            probe.path,
        )
    return Hdr10Metadata(mastering=mastering, content_light=cll, source=source)
