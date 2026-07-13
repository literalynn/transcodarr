"""Détection des capacités au démarrage (arbitrages 5 et 6 du plan validé).

Vérifie, avec de vrais encodages de mire de 5 s :
- la présence des encodeurs QSV/VAAPI (HEVC + AV1) sur chaque GPU configuré,
  avec fallback automatique QSV→VAAPI loggé (§5.3 point 4) ;
- libplacebo et son option `apply_dolbyvision` (`ffmpeg -h filter=libplacebo`) —
  sans elle, AUCUN fichier DV P5 n'est traité (§5.3.1) ;
- la chaîne zéro-copie Vulkan→QSV/VAAPI pour le P5 (repli logiciel sinon) ;
- dovi_tool et mkvpropedit (injection MDCV/CLL conteneur, arbitrage 1).

Le résultat est exposé par `GET /capabilities` et consommé par la queue.
"""

from __future__ import annotations

import asyncio
import glob
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from transcoder.core.dovi import DvCapabilities
from transcoder.enums import Codec, GpuApi

logger = logging.getLogger(__name__)

_MIRE_INPUT = ["-f", "lavfi", "-i", "testsrc2=duration=5:size=1920x1080:rate=25"]
_MIRE_SHORT = ["-f", "lavfi", "-i", "testsrc2=duration=1:size=640x360:rate=25"]


@dataclass(frozen=True, slots=True)
class EncoderTest:
    """Résultat d'un encodage de mire sur un couple (encodeur, GPU)."""

    encoder: str
    ok: bool
    error: str | None = None


@dataclass(slots=True)
class GpuCapability:
    """Capacités effectives d'un GPU après tests de démarrage."""

    gpu_id: int
    name: str
    drm_node: str
    configured_api: GpuApi
    effective_api: GpuApi
    fell_back_to_vaapi: bool
    encoders: dict[str, EncoderTest] = field(default_factory=dict)
    p5_zero_copy: bool = False

    def supports(self, codec: Codec) -> bool:
        enc = f"{codec.value}_{self.effective_api.value}"
        test = self.encoders.get(enc)
        return test is not None and test.ok

    def as_dict(self) -> dict[str, Any]:
        return {
            "gpu_id": self.gpu_id,
            "name": self.name,
            "drm_node": self.drm_node,
            "configured_api": self.configured_api.value,
            "effective_api": self.effective_api.value,
            "fell_back_to_vaapi": self.fell_back_to_vaapi,
            "encoders": {
                name: {"ok": t.ok, "error": t.error} for name, t in self.encoders.items()
            },
            "p5_zero_copy": self.p5_zero_copy,
        }


@dataclass(slots=True)
class Capabilities:
    """Capacités globales de l'instance, calculées au démarrage."""

    ffmpeg_ok: bool
    ffmpeg_version: str | None
    ffprobe_ok: bool
    libplacebo: bool
    libplacebo_dv: bool
    dovi_tool_ok: bool
    mkvpropedit_ok: bool
    gpus: list[GpuCapability] = field(default_factory=list)

    @property
    def dv_caps(self) -> DvCapabilities:
        return DvCapabilities(libplacebo_dv=self.libplacebo_dv, libplacebo=self.libplacebo)

    def gpu(self, gpu_id: int) -> GpuCapability | None:
        return next((g for g in self.gpus if g.gpu_id == gpu_id), None)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ffmpeg_ok": self.ffmpeg_ok,
            "ffmpeg_version": self.ffmpeg_version,
            "ffprobe_ok": self.ffprobe_ok,
            "libplacebo": self.libplacebo,
            "libplacebo_dv": self.libplacebo_dv,
            "dovi_tool_ok": self.dovi_tool_ok,
            "mkvpropedit_ok": self.mkvpropedit_ok,
            "gpus": [g.as_dict() for g in self.gpus],
        }


# ---------------------------------------------------------------------------
# Parsers purs (testables sans binaire)
# ---------------------------------------------------------------------------
def parse_ffmpeg_version(output: str) -> str | None:
    m = re.search(r"ffmpeg version (\S+)", output)
    return m.group(1) if m else None


def parse_libplacebo_help(output: str) -> tuple[bool, bool]:
    """(filtre présent, option apply_dolbyvision présente) — arbitrage 5."""
    present = "libplacebo" in output and "Unknown filter" not in output
    has_dv = present and "apply_dolbyvision" in output
    return present, has_dv


# ---------------------------------------------------------------------------
# Exécution
# ---------------------------------------------------------------------------
async def _run(args: list[str], timeout: float = 120.0) -> tuple[int, str]:
    """Exécute un binaire, retourne (code, stdout+stderr). -1 si introuvable."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except OSError as exc:
        return -1, str(exc)
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return -2, "timeout"
    return proc.returncode if proc.returncode is not None else -1, stdout.decode(
        "utf-8", "replace"
    )


def _hw_args(api: GpuApi, drm_node: str) -> list[str]:
    if api is GpuApi.QSV:
        return ["-init_hw_device", f"qsv=hw:{drm_node}", "-filter_hw_device", "hw"]
    return ["-init_hw_device", f"vaapi=va:{drm_node}", "-filter_hw_device", "va"]


async def run_encoder_test(
    ffmpeg_path: str, *, codec: Codec, api: GpuApi, drm_node: str
) -> EncoderTest:
    """Encode 5 s de mire (testsrc2) sur le GPU — le test du bouton UI (§9.5.4)."""
    encoder = f"{codec.value}_{api.value}"
    vf = ["-vf", "format=nv12,hwupload"] if api is GpuApi.VAAPI else ["-pix_fmt", "nv12"]
    code, output = await _run(
        [
            ffmpeg_path,
            "-hide_banner",
            "-nostdin",
            "-v",
            "error",
            *_hw_args(api, drm_node),
            *_MIRE_INPUT,
            *vf,
            "-c:v",
            encoder,
            "-b:v",
            "3000k",
            "-f",
            "null",
            "-",
        ]
    )
    if code == 0:
        return EncoderTest(encoder=encoder, ok=True)
    return EncoderTest(encoder=encoder, ok=False, error=output.strip()[-500:] or f"code {code}")


async def check_p5_zero_copy(ffmpeg_path: str, *, api: GpuApi, drm_node: str) -> bool:
    """Teste la chaîne libplacebo Vulkan→QSV/VAAPI du §5.3.1 (arbitrage 6)."""
    if api is GpuApi.QSV:
        hw = [
            "-init_hw_device", "vulkan=vk",
            "-init_hw_device", f"qsv=hw:{drm_node}",
            "-filter_hw_device", "vk",
        ]
        vf = "libplacebo=format=p010le,hwmap=derive_device=qsv,format=qsv"
        encoder = "hevc_qsv"
    else:
        hw = [
            "-init_hw_device", f"vaapi=va:{drm_node}",
            "-init_hw_device", "vulkan=vk@va",
            "-filter_hw_device", "vk",
        ]
        vf = "libplacebo=format=p010le,hwmap=derive_device=vaapi"
        encoder = "hevc_vaapi"
    code, _output = await _run(
        [
            ffmpeg_path,
            "-hide_banner",
            "-nostdin",
            "-v",
            "error",
            *hw,
            *_MIRE_SHORT,
            "-vf",
            vf,
            "-c:v",
            encoder,
            "-b:v",
            "1000k",
            "-f",
            "null",
            "-",
        ]
    )
    return code == 0


@dataclass(frozen=True, slots=True)
class GpuConfig:
    """Sous-ensemble du modèle Gpu nécessaire aux tests (découplage ORM)."""

    id: int
    name: str
    drm_node: str
    api: GpuApi


async def probe_gpu(ffmpeg_path: str, gpu: GpuConfig, *, libplacebo: bool) -> GpuCapability:
    """Teste HEVC+AV1 sur l'API configurée ; fallback QSV→VAAPI loggé (§5.3.4)."""
    cap = GpuCapability(
        gpu_id=gpu.id,
        name=gpu.name,
        drm_node=gpu.drm_node,
        configured_api=gpu.api,
        effective_api=gpu.api,
        fell_back_to_vaapi=False,
    )
    for codec in (Codec.HEVC, Codec.AV1):
        test = await run_encoder_test(ffmpeg_path, codec=codec, api=gpu.api, drm_node=gpu.drm_node)
        cap.encoders[test.encoder] = test

    hevc_key = f"hevc_{gpu.api.value}"
    if gpu.api is GpuApi.QSV and not cap.encoders[hevc_key].ok:
        logger.warning(
            "GPU %s (%s) : hevc_qsv indisponible (%s) — fallback automatique VAAPI",
            gpu.name,
            gpu.drm_node,
            cap.encoders[hevc_key].error,
        )
        vaapi_ok = False
        for codec in (Codec.HEVC, Codec.AV1):
            test = await run_encoder_test(
                ffmpeg_path, codec=codec, api=GpuApi.VAAPI, drm_node=gpu.drm_node
            )
            cap.encoders[test.encoder] = test
            if codec is Codec.HEVC and test.ok:
                vaapi_ok = True
        if vaapi_ok:
            cap.effective_api = GpuApi.VAAPI
            cap.fell_back_to_vaapi = True

    if libplacebo and cap.encoders.get(f"hevc_{cap.effective_api.value}", EncoderTest("", False)).ok:
        cap.p5_zero_copy = await check_p5_zero_copy(
            ffmpeg_path, api=cap.effective_api, drm_node=gpu.drm_node
        )
        if not cap.p5_zero_copy:
            logger.warning(
                "GPU %s : chaîne zéro-copie Vulkan→%s indisponible — "
                "repli logiciel (libplacebo → RAM → encodeur) pour le DV P5",
                gpu.name,
                cap.effective_api.value,
            )
    return cap


async def detect_capabilities(
    *,
    ffmpeg_path: str,
    ffprobe_path: str,
    dovi_tool_path: str,
    mkvpropedit_path: str,
    gpus: list[GpuConfig],
) -> Capabilities:
    """Détection complète au démarrage. Ne lève jamais : tout échec est un flag."""
    code, out = await _run([ffmpeg_path, "-version"], timeout=15)
    ffmpeg_ok = code == 0
    version = parse_ffmpeg_version(out) if ffmpeg_ok else None

    probe_code, _ = await _run([ffprobe_path, "-version"], timeout=15)
    ffprobe_ok = probe_code == 0

    libplacebo = libplacebo_dv = False
    if ffmpeg_ok:
        _, help_out = await _run([ffmpeg_path, "-hide_banner", "-h", "filter=libplacebo"], 15)
        libplacebo, libplacebo_dv = parse_libplacebo_help(help_out)
        if not libplacebo_dv:
            logger.warning(
                "libplacebo sans support Dolby Vision (option apply_dolbyvision absente) : "
                "les fichiers DV P5 seront marqués skipped_policy, jamais traités en best-effort"
            )

    dovi_code, _ = await _run([dovi_tool_path, "--version"], timeout=15)
    mkv_code, _ = await _run([mkvpropedit_path, "--version"], timeout=15)

    caps = Capabilities(
        ffmpeg_ok=ffmpeg_ok,
        ffmpeg_version=version,
        ffprobe_ok=ffprobe_ok,
        libplacebo=libplacebo,
        libplacebo_dv=libplacebo_dv,
        dovi_tool_ok=dovi_code == 0,
        mkvpropedit_ok=mkv_code == 0,
    )
    if not ffmpeg_ok:
        logger.error("FFmpeg introuvable (%s) : aucun encodage possible", ffmpeg_path)
        return caps

    for gpu in gpus:
        caps.gpus.append(await probe_gpu(ffmpeg_path, gpu, libplacebo=libplacebo))
    return caps


def detect_drm_nodes() -> list[str]:
    """Auto-détection des nodes DRM (`GET /gpus/detect`, §7.6)."""
    return sorted(glob.glob("/dev/dri/renderD*"))
