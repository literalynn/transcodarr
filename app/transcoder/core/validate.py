"""Validation post-encode : les 9 contrôles bloquants du §5.5.

AUCUN remplacement sans que tous les contrôles passent. Le rapport structuré
est persisté dans jobs.validation_report. Les contrôles s'arrêtent au premier
échec (inutile de décoder intégralement un fichier au mauvais codec).

Contrôle 9 (anti-vert/violet, arbitrage 2 du plan validé) : la référence
dépend du profil DV — P5 = source décodée avec RPU appliqué (libplacebo) ;
P7/P8.x = couche de base décodée SANS RPU (comparer au rendu RPU-appliqué
créerait des faux positifs à cause des trims DV).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from transcoder.core.dovi import TreatmentAction, TreatmentPlan
from transcoder.core.probe import MediaProbe, ProbeError, probe_file
from transcoder.enums import Codec, HdrFormat

logger = logging.getLogger(__name__)

# Callbacks de suivi : nom du contrôle en cours, et progression 0-100 du
# décodage d'intégrité (contrôle 5, le plus long) — consommés par la queue
# pour les logs, le WebSocket et l'UI.
CheckCallback = Callable[[str], Awaitable[None]]
DecodeProgressCallback = Callable[[float], Awaitable[None]]


class ValidationOutcome(str, Enum):
    PASSED = "passed"
    FAILED = "failed"
    NO_GAIN = "no_gain"  # contrôle 7 : original conservé, job skipped_no_gain


@dataclass(frozen=True, slots=True)
class CheckResult:
    name: str
    ok: bool
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "ok": self.ok, "detail": self.detail}


@dataclass(slots=True)
class ValidationReport:
    outcome: ValidationOutcome
    checks: list[CheckResult] = field(default_factory=list)
    gain_percent: float | None = None
    dst_probe: MediaProbe | None = None

    @property
    def failure_reason(self) -> str | None:
        for c in self.checks:
            if not c.ok:
                return f"{c.name} : {c.detail}"
        return None

    def as_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome.value,
            "gain_percent": self.gain_percent,
            "checks": [c.as_dict() for c in self.checks],
        }


# ---------------------------------------------------------------------------
# Contrôles purs (1 à 4, 6 à 8) — testés sans binaire
# ---------------------------------------------------------------------------
def check_exit_code(exit_code: int) -> CheckResult:
    return CheckResult(
        "1_exit_code",
        exit_code == 0,
        "ffmpeg terminé sans erreur" if exit_code == 0 else f"code de sortie {exit_code}",
    )


def check_output_size(size_bytes: int | None) -> CheckResult:
    ok = size_bytes is not None and size_bytes > 0
    return CheckResult(
        "2_output_exists",
        ok,
        f"{size_bytes} octets" if ok else "fichier de sortie absent ou vide",
    )


def check_codec(dst_probe: MediaProbe, target: Codec) -> CheckResult:
    ok = dst_probe.video_codec == target.value
    return CheckResult(
        "3_codec",
        ok,
        f"codec vidéo = {dst_probe.video_codec}"
        + ("" if ok else f" (attendu {target.value})"),
    )


def check_duration(src_duration_s: float | None, dst_duration_s: float | None) -> CheckResult:
    if src_duration_s is None or dst_duration_s is None:
        return CheckResult("4_duration", False, "durée source ou sortie inconnue")
    tolerance = max(1.0, src_duration_s * 0.005)
    delta = abs(dst_duration_s - src_duration_s)
    return CheckResult(
        "4_duration",
        delta <= tolerance,
        f"Δ={delta:.2f}s (tolérance {tolerance:.2f}s)",
    )


def _copyable(streams: list[dict[str, Any]]) -> int:
    """Pistes effectivement copiables : les codecs inconnus (« none ») sont
    exclus du mapping par le builder — ils ne comptent pas dans l'attendu."""
    return sum(1 for s in streams if s.get("codec") and s.get("codec") != "none")


def check_stream_counts(src_probe: MediaProbe, dst_probe: MediaProbe) -> CheckResult:
    src_a, dst_a = _copyable(src_probe.audio_streams), len(dst_probe.audio_streams)
    src_s, dst_s = _copyable(src_probe.subtitle_streams), len(dst_probe.subtitle_streams)
    ok = src_a == dst_a and src_s == dst_s
    dropped = (
        len(src_probe.audio_streams) + len(src_probe.subtitle_streams) - src_a - src_s
    )
    detail = f"audio {dst_a}/{src_a}, sous-titres {dst_s}/{src_s}"
    if dropped:
        detail += f" ({dropped} piste(s) au codec inconnu exclue(s))"
    return CheckResult("6_streams", ok, detail)


def check_gain(
    src_size: int, dst_size: int, min_gain_percent: float
) -> tuple[CheckResult, float, bool]:
    """Retourne (résultat, gain_percent, no_gain). no_gain n'est PAS un échec :
    l'original est conservé et le job passe en skipped_no_gain (§5.5.7)."""
    gain = (1.0 - dst_size / src_size) * 100.0 if src_size > 0 else 0.0
    no_gain = dst_size >= src_size * (1.0 - min_gain_percent / 100.0)
    return (
        CheckResult(
            "7_gain",
            True,
            f"gain {gain:.1f}% (seuil {min_gain_percent:.1f}%)"
            + (" → original conservé" if no_gain else ""),
        ),
        round(gain, 2),
        no_gain,
    )


def check_colorimetry(dst_probe: MediaProbe, expected: HdrFormat) -> CheckResult:
    """Contrôle 8 : flags couleur, 10-bit, MDCV+CLL présents (§5.5.8).

    Vérifié via ffprobe sur la sortie — pour un MKV, la side data reflète
    aussi bien le SEI bitstream (HEVC) que le MasteringMetadata conteneur
    (AV1, arbitrage 1).
    """
    problems: list[str] = []
    if dst_probe.color_primaries != "bt2020":
        problems.append(f"primaries={dst_probe.color_primaries}")
    expected_trc = "arib-std-b67" if expected is HdrFormat.HLG else "smpte2084"
    if dst_probe.color_transfer != expected_trc:
        problems.append(f"trc={dst_probe.color_transfer} (attendu {expected_trc})")
    if dst_probe.color_space != "bt2020nc":
        problems.append(f"colorspace={dst_probe.color_space}")
    if dst_probe.bit_depth is None or dst_probe.bit_depth < 10:
        problems.append(f"bit_depth={dst_probe.bit_depth}")
    if expected is not HdrFormat.HLG:
        if dst_probe.mastering_display is None:
            problems.append("MDCV absent")
        if dst_probe.content_light is None:
            problems.append("MaxCLL/MaxFALL absents")
    return CheckResult(
        "8_colorimetry",
        not problems,
        "flags BT.2020 + métadonnées HDR présents" if not problems else ", ".join(problems),
    )


def check_no_dovi(dst_probe: MediaProbe) -> CheckResult:
    """§5.3.1 « nettoyage du bitstream » : aucun side_data DOVI en sortie."""
    ok = dst_probe.dv_profile is None
    return CheckResult(
        "8b_no_dovi",
        ok,
        "aucune configuration DOVI résiduelle"
        if ok
        else f"DOVI configuration record présent (profil {dst_probe.dv_profile})",
    )


# ---------------------------------------------------------------------------
# Parsers purs pour le contrôle 9
# ---------------------------------------------------------------------------
def parse_ssim_all(output: str) -> float | None:
    """Extrait le SSIM global de la sortie du filtre ssim (`All:0.987654`)."""
    m = re.search(r"All:([0-9.]+)", output)
    return float(m.group(1)) if m else None


def parse_signalstats(output: str) -> tuple[float, float] | None:
    """Extrait (UAVG, VAVG) de la sortie metadata=print de signalstats."""
    u = re.search(r"lavfi\.signalstats\.UAVG=([0-9.]+)", output)
    v = re.search(r"lavfi\.signalstats\.VAVG=([0-9.]+)", output)
    if u is None or v is None:
        return None
    return float(u.group(1)), float(v.group(1))


def reference_decode_filter(plan: TreatmentPlan) -> str | None:
    """Chaîne de décodage de RÉFÉRENCE de la source DV (arbitrage 2).

    P5 : RPU appliqué (libplacebo) → signal BT.2020 PQ reconstruit.
    P7/P8.x : couche de base telle quelle (SANS RPU) — c'est elle que
    l'encodage a reprise, la comparer au rendu RPU-appliqué créerait des
    faux positifs (trims DV).
    """
    if plan.needs_rpu_applied:
        return (
            "libplacebo=apply_dolbyvision=true:colorspace=bt2020nc:"
            "color_primaries=bt2020:color_trc=smpte2084:range=tv:format=p010le"
        )
    return None


# ---------------------------------------------------------------------------
# Exécution
# ---------------------------------------------------------------------------
async def _run_tool(args: list[str], timeout: float = 3600.0) -> tuple[int, str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            stdin=asyncio.subprocess.DEVNULL,
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


async def _run_ffmpeg_with_progress(
    args: list[str],
    *,
    duration_s: float | None,
    on_progress: DecodeProgressCallback | None,
    timeout: float = 7200.0,
) -> tuple[int, str]:
    """FFmpeg avec parse de `-progress pipe:1` : (code, stderr). Permet de
    suivre le décodage d'intégrité au lieu d'un silence de plusieurs minutes."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.DEVNULL,
        )
    except OSError as exc:
        return -1, str(exc)
    assert proc.stdout is not None and proc.stderr is not None

    async def consume_progress() -> None:
        assert proc.stdout is not None
        last_emitted = -10.0
        async for raw in proc.stdout:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("out_time_us=") or duration_s is None or duration_s <= 0:
                continue
            try:
                percent = min(100.0, int(line.split("=", 1)[1]) / 1_000_000.0 / duration_s * 100.0)
            except ValueError:
                continue
            if percent - last_emitted >= 5.0 and on_progress is not None:
                last_emitted = percent
                await on_progress(percent)

    async def consume_stderr() -> bytes:
        assert proc.stderr is not None
        return await proc.stderr.read()

    try:
        progress_task = asyncio.create_task(consume_progress())
        stderr_task = asyncio.create_task(consume_stderr())
        await asyncio.wait_for(proc.wait(), timeout=timeout)
        await progress_task
        stderr = await stderr_task
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return -2, "timeout"
    return proc.returncode if proc.returncode is not None else -1, stderr.decode(
        "utf-8", "replace"
    )


async def _check_decode_integrity(
    ffmpeg_path: str,
    dst_path: str,
    *,
    mode: str,
    duration_s: float | None,
    on_progress: DecodeProgressCallback | None = None,
) -> CheckResult:
    """Contrôle 5 : décodage CPU complet (ou 3 segments de 30 s si `sample`)."""
    if mode == "sample" and duration_s and duration_s > 120:
        for ratio in (0.1, 0.5, 0.9):
            start = duration_s * ratio
            code, out = await _run_tool(
                [
                    ffmpeg_path, "-hide_banner", "-nostdin",
                    "-v", "error", "-xerror",
                    "-ss", f"{start:.2f}", "-t", "30",
                    "-i", dst_path,
                    "-f", "null", "-",
                ]
            )
            if code != 0 or out.strip():
                return CheckResult(
                    "5_integrity", False, f"erreur de décodage à {start:.0f}s : {out.strip()[:300]}"
                )
        return CheckResult("5_integrity", True, "3 segments de 30 s décodés sans erreur")

    code, err = await _run_ffmpeg_with_progress(
        [
            ffmpeg_path, "-hide_banner", "-nostdin",
            "-v", "error", "-xerror",
            "-progress", "pipe:1", "-nostats",
            "-i", dst_path,
            "-f", "null", "-",
        ],
        duration_s=duration_s,
        on_progress=on_progress,
    )
    ok = code == 0 and not err.strip()
    return CheckResult(
        "5_integrity",
        ok,
        "décodage intégral sans erreur" if ok else f"erreurs : {err.strip()[:300]}",
    )


async def _extract_frame(
    ffmpeg_path: str, media_path: str, ts: float, out_png: str, pre_filter: str | None
) -> bool:
    vf = f"{pre_filter}," if pre_filter else ""
    vf += "scale=960:-2,format=yuv420p"
    code, _ = await _run_tool(
        [
            ffmpeg_path, "-hide_banner", "-nostdin", "-y",
            "-v", "error",
            "-ss", f"{ts:.3f}",
            "-i", media_path,
            "-map", "0:v:0",
            "-frames:v", "1",
            "-vf", vf,
            out_png,
        ],
        timeout=300.0,
    )
    return code == 0 and os.path.exists(out_png)


async def _compare_frames(
    ffmpeg_path: str, ref_png: str, out_png: str
) -> tuple[float | None, float | None, float | None]:
    """Retourne (ssim, delta_u, delta_v) entre deux frames extraites."""
    code, ssim_out = await _run_tool(
        [
            ffmpeg_path, "-hide_banner", "-nostdin",
            "-i", ref_png, "-i", out_png,
            "-filter_complex", "[0:v]format=yuv420p[a];[1:v]format=yuv420p[b];[a][b]ssim",
            "-f", "null", "-",
        ],
        timeout=120.0,
    )
    ssim = parse_ssim_all(ssim_out) if code == 0 else None

    stats: list[tuple[float, float]] = []
    for png in (ref_png, out_png):
        code, out = await _run_tool(
            [
                ffmpeg_path, "-hide_banner", "-nostdin",
                "-i", png,
                "-vf", "format=yuv420p,signalstats,metadata=mode=print:file=-",
                "-f", "null", "-",
            ],
            timeout=120.0,
        )
        parsed = parse_signalstats(out) if code == 0 else None
        if parsed is None:
            return ssim, None, None
        stats.append(parsed)
    delta_u = abs(stats[0][0] - stats[1][0])
    delta_v = abs(stats[0][1] - stats[1][1])
    return ssim, delta_u, delta_v


async def _check_dv_colors(
    *,
    ffmpeg_path: str,
    src_path: str,
    dst_path: str,
    duration_s: float | None,
    plan: TreatmentPlan,
    ssim_threshold: float,
    chroma_delta_max: float,
    tmp_dir: str,
) -> CheckResult:
    """Contrôle 9 : anti-« image verte et violette » (§5.5.9)."""
    if not duration_s or duration_s <= 0:
        return CheckResult("9_dv_colors", False, "durée inconnue : comparaison impossible")

    pre_filter = reference_decode_filter(plan)
    os.makedirs(tmp_dir, exist_ok=True)
    details: list[str] = []
    try:
        for ratio in (0.1, 0.5, 0.9):
            ts = duration_s * ratio
            ref_png = os.path.join(tmp_dir, f"vref-{uuid.uuid4().hex}.png")
            out_png = os.path.join(tmp_dir, f"vout-{uuid.uuid4().hex}.png")
            try:
                ok_ref = await _extract_frame(ffmpeg_path, src_path, ts, ref_png, pre_filter)
                ok_out = await _extract_frame(ffmpeg_path, dst_path, ts, out_png, None)
                if not ok_ref or not ok_out:
                    return CheckResult(
                        "9_dv_colors", False, f"extraction de frame impossible à {ts:.0f}s"
                    )
                ssim, delta_u, delta_v = await _compare_frames(ffmpeg_path, ref_png, out_png)
                if ssim is None or delta_u is None or delta_v is None:
                    return CheckResult(
                        "9_dv_colors", False, f"métriques indisponibles à {ts:.0f}s"
                    )
                details.append(
                    f"t={ts:.0f}s ssim={ssim:.4f} ΔU={delta_u:.2f} ΔV={delta_v:.2f}"
                )
                if ssim < ssim_threshold:
                    return CheckResult(
                        "9_dv_colors",
                        False,
                        f"conversion DV invalide : SSIM {ssim:.4f} < {ssim_threshold} "
                        f"à {ts:.0f}s",
                    )
                if delta_u > chroma_delta_max or delta_v > chroma_delta_max:
                    return CheckResult(
                        "9_dv_colors",
                        False,
                        f"conversion DV invalide : écart chroma ΔU={delta_u:.2f} "
                        f"ΔV={delta_v:.2f} > {chroma_delta_max} à {ts:.0f}s "
                        "(signature d'une casse IPT→BT.2020)",
                    )
            finally:
                for p in (ref_png, out_png):
                    with contextlib.suppress(OSError):
                        os.unlink(p)
    except OSError as exc:
        return CheckResult("9_dv_colors", False, f"erreur d'E/S : {exc}")
    return CheckResult("9_dv_colors", True, " ; ".join(details))


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
async def validate_output(
    *,
    ffmpeg_path: str,
    ffprobe_path: str,
    src_probe: MediaProbe,
    src_size_bytes: int,
    dst_path: str,
    exit_code: int,
    plan: TreatmentPlan,
    target_codec: Codec,
    min_gain_percent: float,
    validation_decode: str,
    dv_ssim_threshold: float,
    dv_chroma_delta_max: float,
    tmp_dir: str,
    on_check: CheckCallback | None = None,
    on_decode_progress: DecodeProgressCallback | None = None,
) -> ValidationReport:
    """Exécute les 9 contrôles §5.5 dans l'ordre, arrêt au premier échec.

    `on_check` est appelé au DÉBUT de chaque contrôle (affichage UI/logs),
    `on_decode_progress` pendant le décodage d'intégrité (contrôle 5)."""
    report = ValidationReport(outcome=ValidationOutcome.FAILED)

    async def _step(label: str) -> None:
        logger.info("Validation %s : %s", os.path.basename(dst_path), label)
        if on_check is not None:
            await on_check(label)

    def _fail(check: CheckResult) -> ValidationReport:
        report.checks.append(check)
        report.outcome = ValidationOutcome.FAILED
        logger.warning(
            "Validation %s : ÉCHEC %s — %s", os.path.basename(dst_path), check.name, check.detail
        )
        return report

    # 1 — exit code
    await _step("1/9 code de sortie ffmpeg")
    c1 = check_exit_code(exit_code)
    if not c1.ok:
        return _fail(c1)
    report.checks.append(c1)

    # 2 — sortie présente et non vide
    await _step("2/9 présence et taille de la sortie")
    try:
        dst_size = os.path.getsize(dst_path)
    except OSError:
        dst_size = 0
    c2 = check_output_size(dst_size if dst_size > 0 else None)
    if not c2.ok:
        return _fail(c2)
    report.checks.append(c2)

    # 3 — sonde de la sortie + codec cible
    await _step("3/9 sonde ffprobe + codec cible")
    try:
        dst_probe = await probe_file(dst_path, ffprobe_path)
    except ProbeError as exc:
        return _fail(CheckResult("3_codec", False, f"ffprobe a échoué : {exc}"))
    report.dst_probe = dst_probe
    c3 = check_codec(dst_probe, target_codec)
    if not c3.ok:
        return _fail(c3)
    report.checks.append(c3)

    # 4 — durée
    await _step("4/9 durée source/sortie")
    c4 = check_duration(src_probe.duration_s, dst_probe.duration_s)
    if not c4.ok:
        return _fail(c4)
    report.checks.append(c4)

    # 5 — intégrité (décodage CPU — le contrôle le plus long, suivi en %)
    await _step("5/9 intégrité (décodage complet)")
    c5 = await _check_decode_integrity(
        ffmpeg_path,
        dst_path,
        mode=validation_decode,
        duration_s=dst_probe.duration_s,
        on_progress=on_decode_progress,
    )
    if not c5.ok:
        return _fail(c5)
    report.checks.append(c5)

    # 6 — complétude des flux
    await _step("6/9 complétude des pistes")
    c6 = check_stream_counts(src_probe, dst_probe)
    if not c6.ok:
        return _fail(c6)
    report.checks.append(c6)

    # 7 — gain (pas un échec : skipped_no_gain, original conservé)
    await _step("7/9 gain de taille")
    c7, gain, no_gain = check_gain(src_size_bytes, dst_size, min_gain_percent)
    report.checks.append(c7)
    report.gain_percent = gain
    if no_gain:
        report.outcome = ValidationOutcome.NO_GAIN
        return report

    # 8 — colorimétrie (si la sortie doit être HDR) + absence de DOVI (toujours)
    if plan.output_hdr_format in (HdrFormat.HDR10, HdrFormat.HLG):
        await _step("8/9 colorimétrie HDR")
        c8 = check_colorimetry(dst_probe, plan.output_hdr_format)
        if not c8.ok:
            return _fail(c8)
        report.checks.append(c8)
    c8b = check_no_dovi(dst_probe)
    if not c8b.ok:
        return _fail(c8b)
    report.checks.append(c8b)

    # 9 — anti-vert/violet (obligatoire si la source était Dolby Vision)
    if plan.source_is_dv:
        await _step("9/9 contrôle couleurs DV (anti-vert/violet)")
        c9 = await _check_dv_colors(
            ffmpeg_path=ffmpeg_path,
            src_path=src_probe.path,
            dst_path=dst_path,
            duration_s=src_probe.duration_s,
            plan=plan,
            ssim_threshold=dv_ssim_threshold,
            chroma_delta_max=dv_chroma_delta_max,
            tmp_dir=tmp_dir,
        )
        if not c9.ok:
            return _fail(c9)
        report.checks.append(c9)

    report.outcome = ValidationOutcome.PASSED
    logger.info(
        "Validation %s : les %d contrôles sont PASSÉS (gain %.1f%%)",
        os.path.basename(dst_path),
        len(report.checks),
        report.gain_percent or 0.0,
    )
    return report
