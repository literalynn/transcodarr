"""Queue et workers asyncio (§5.2) + pipeline complet d'un job.

- slots = Σ worker_count des GPU activés, plafonnés par max_concurrent_jobs ;
- claim par `SELECT … FOR UPDATE SKIP LOCKED` (résistant au redémarrage) ;
- ordonnancement : manual_rank (drag §7.2), puis priority DESC, puis gain
  potentiel estimé, puis mtime DESC — le filtre de date est appliqué au scan ;
- pause globale (app_settings.queue_paused) et par bibliothèque (paused) ;
- reprise après crash : running/validating/replacing → queued + purge tmp.

Pipeline d'un job (§5.3→§5.6) : pré-vol (source lisible et stable, sonde,
gain atteignable, test de décodage) → plan DV → règle → plan de pistes →
commande selon la stratégie → encodage (progression WS + snapshots) →
injection MDCV/CLL conteneur si nécessaire → les 9 validations →
remplacement atomique (ou sortie dry-run).

Après un échec (core/failures.py, core/strategy.py), trois issues :
- **repli** : la catégorie de l'échec indique une stratégie plus robuste
  pas encore tentée → nouvelle tentative immédiate ;
- **nouvel essai** : échec dû à l'environnement (arrêt, disque, fichier en
  cours de copie) → même stratégie après backoff, dans la limite de
  max_retries ;
- **définitif** : plus rien à essayer → `failed`, et le scan ne le remet
  PLUS en file tant que ni le fichier ni la règle ne changent (l'ancienne
  logique relançait les échecs à chaque scan : jusqu'à 161 tentatives).
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import decimal
import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from transcoder import TOOL_ID
from transcoder.core.capabilities import Capabilities, GpuCapability, mkvmerge_path_for
from transcoder.core.cmd_builder import BuildError, BuildParams, build_ffmpeg_cmd
from transcoder.core.dovi import (
    TreatmentAction,
    TreatmentPlan,
    plan_treatment,
    resolve_output_hdr10_metadata,
)
from transcoder.core.failures import (
    Failure,
    FailureCategory,
    classify_legacy,
    classify_validation_failure,
    key_error_lines,
)
from transcoder.core.mkvmerge import (
    AssemblyError,
    identify_tracks,
    intermediate_path_for,
    is_matroska,
    mkvmerge_args,
    plan_assembly,
    run_mkvmerge,
)
from transcoder.core.preflight import (
    PreflightReport,
    check_source_access,
    predict_gain,
    quick_decode_test,
)
from transcoder.core.probe import (
    MediaProbe,
    ProbeError,
    is_legacy_timestamp_format,
    probe_file,
    probe_stream_timeline,
)
from transcoder.core.replace import (
    DiskSpaceError,
    ReplaceError,
    add_track_statistics_mkv,
    check_free_space,
    inject_hdr10_metadata_mkv,
    make_dry_run_path,
    make_tmp_path,
    purge_orphan_tmp,
    replace_original,
)
from transcoder.core.rules import NoRuleError, ResolvedRule, build_transcoder_tag, resolve_rule
from transcoder.core.runner import ProgressUpdate, run_ffmpeg
from transcoder.core.stream_plan import (
    StreamAction,
    StreamPlan,
    plan_signature,
    plan_streams,
    streams_from_diagnostics,
)
from transcoder.core.strategy import (
    Strategy,
    is_transient,
    next_strategy,
    parse_strategy,
)
from transcoder.core.validate import ValidationOutcome, ValidationReport, validate_output
from transcoder.db import session_scope
from transcoder.enums import (
    Codec,
    Container,
    DvPolicy,
    GpuApi,
    HdrFormat,
    HdrPolicy,
    JobState,
    MediaStatus,
    ResolutionBucket,
)
from transcoder.events import event_bus
from transcoder.models import BitrateRule, Job, JobProgressSnapshot, Library, MediaFile
from transcoder.runtime_config import RuntimeConfig, load_runtime_config

logger = logging.getLogger(__name__)

_IDLE_POLL_S = 5.0
# Garde-fou absolu : au-delà, un fichier n'est plus retenté automatiquement
MAX_ATTEMPTS_PER_SERIES = 6


def retry_backoff_s(retry_count: int) -> float:
    """Backoff exponentiel : 5 min, 10 min, 20 min… (§5.2)."""
    return 300.0 * float(2 ** max(0, retry_count - 1))


def _now() -> datetime.datetime:
    return datetime.datetime.now(tz=datetime.timezone.utc)


@dataclass(slots=True)
class _ClaimedWork:
    job_id: int
    media_id: int
    library_id: int
    attempt: int = 1
    strategy: Strategy = Strategy.STANDARD
    # « Forcer le réencodage » : ignore la prédiction de gain
    forced: bool = False


@dataclass(slots=True)
class _Prepared:
    """Tout ce que la préparation transmet à l'encodage."""

    probe: MediaProbe
    rule: ResolvedRule
    plan: TreatmentPlan
    stream_plan: StreamPlan
    container: Container
    min_gain_percent: float
    cmd: list[str]
    tmp_path: str
    dry_run: bool
    log_path: str
    gpu_api: GpuApi
    diagnostics: dict[str, Any] = field(default_factory=dict)
    # Taille du log du job avant cette tentative : la validation 6b ne lit
    # que la suite (un job repris après crash réutilise le même log)
    log_offset: int = 0
    # Assemblage mkvmerge (stratégie mkvmerge_mux) : FFmpeg écrit un
    # intermédiaire, mkvmerge produit tmp_path
    intermediate_path: str | None = None
    mkvmerge_cmd: list[str] | None = None


@dataclass(frozen=True, slots=True)
class FailureDecision:
    """Issue d'un échec pour le fichier : repli, nouvel essai ou définitif."""

    kind: str  # fallback / retry / final
    next_strategy: Strategy | None
    retry_in_s: float | None
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "next_strategy": self.next_strategy.value if self.next_strategy else None,
            "retry_in_s": self.retry_in_s,
            "reason": self.reason,
        }


def decide_after_failure(
    category: FailureCategory,
    *,
    tried: list[str],
    retry_count: int,
    attempt_count: int,
    current: Strategy,
    max_retries: int,
    auto_fallback: bool,
    alt_api_available: bool,
    changes_command: Callable[[Strategy], bool] | None = None,
) -> FailureDecision:
    """Décision pure après un échec (testée sans base ni GPU)."""
    if attempt_count >= MAX_ATTEMPTS_PER_SERIES:
        return FailureDecision(
            "final", None, None,
            f"{attempt_count} tentatives dans la série : arrêt des essais automatiques",
        )
    if is_transient(category):
        if retry_count + 1 > max_retries:
            return FailureDecision(
                "final", None, None,
                f"échec transitoire répété ({retry_count + 1} fois) : relance manuelle requise",
            )
        delay = retry_backoff_s(retry_count + 1)
        return FailureDecision(
            "retry", current, delay,
            f"échec lié à l'environnement : nouvel essai dans {int(delay // 60)} min",
        )
    if auto_fallback:
        nxt = next_strategy(
            category, tried, alt_api_available=alt_api_available, changes_command=changes_command
        )
        if nxt is not None:
            return FailureDecision(
                "fallback", nxt, 0.0, f"nouvelle tentative avec la stratégie « {nxt.value} »"
            )
    return FailureDecision(
        "final", None, None,
        "aucune autre méthode applicable à cette erreur : ne sera plus retenté automatiquement",
    )


def _reset_series(mf: MediaFile) -> None:
    """Fin de série (succès, sans gain, fichier remplacé) : compteurs à zéro."""
    mf.retry_count = 0
    mf.attempt_count = 0
    mf.strategies_tried = []
    mf.next_strategy = None
    mf.failure_category = None
    mf.next_attempt_at = None


class _PrepareAbort(Exception):
    """Préparation interrompue : l'issue a déjà été enregistrée."""


class QueueManager:
    """Workers asyncio + cycle de vie des jobs. Une instance par process."""

    def __init__(self, capabilities: Capabilities) -> None:
        self.capabilities = capabilities
        self._workers: list[asyncio.Task[None]] = []
        self._cancel_events: dict[int, asyncio.Event] = {}
        self._active_jobs: dict[int, dict[str, Any]] = {}
        self._stopping = asyncio.Event()

    # ------------------------------------------------------------------
    # Cycle de vie
    # ------------------------------------------------------------------
    async def start(self) -> None:
        """Démarre un worker par slot (Σ worker_count des GPU activés)."""
        await self.stop()
        self._stopping.clear()
        for gpu_cap in self.capabilities.gpus:
            gpu_row = await self._load_gpu_row(gpu_cap.gpu_id)
            if gpu_row is None or not gpu_row["enabled"]:
                continue
            for slot in range(gpu_row["worker_count"]):
                self._workers.append(
                    asyncio.create_task(
                        self._worker_loop(gpu_cap, slot),
                        name=f"worker-{gpu_cap.drm_node}-{slot}",
                    )
                )
        logger.info("Queue démarrée : %d worker(s)", len(self._workers))

    async def stop(self) -> None:
        self._stopping.set()
        for t in self._workers:
            t.cancel()
        for t in self._workers:
            with contextlib.suppress(asyncio.CancelledError):
                await t
        self._workers = []

    async def reload_workers(self) -> None:
        """À appeler quand la config GPU change (workers/enable)."""
        await self.start()

    @staticmethod
    async def _load_gpu_row(gpu_id: int) -> dict[str, Any] | None:
        from transcoder.models import Gpu

        async with session_scope() as session:
            gpu = await session.get(Gpu, gpu_id)
            if gpu is None:
                return None
            return {"enabled": gpu.enabled, "worker_count": gpu.worker_count}

    # ------------------------------------------------------------------
    # Actions exposées à l'API
    # ------------------------------------------------------------------
    def cancel_job(self, job_id: int) -> bool:
        event = self._cancel_events.get(job_id)
        if event is None:
            return False
        event.set()
        return True

    def _set_phase(self, job_id: int, phase: str) -> None:
        """Phase lisible du job (pré-vol / encodage / validation x/9 /
        remplacement) : bandeau actif, événement WS et ligne de log."""
        info = self._active_jobs.get(job_id)
        if info is not None:
            info["phase"] = phase
        logger.info("Job %d : %s", job_id, phase)
        event_bus.publish_queue({"type": "phase", "job_id": job_id, "phase": phase})

    def active_jobs(self) -> list[dict[str, Any]]:
        return [dict(v) for v in self._active_jobs.values()]

    def worker_count(self) -> int:
        return len(self._workers)

    # ------------------------------------------------------------------
    # Boucle worker
    # ------------------------------------------------------------------
    async def _worker_loop(self, gpu_cap: GpuCapability, slot: int) -> None:
        while not self._stopping.is_set():
            try:
                async with session_scope() as session:
                    config = await load_runtime_config(session)
                if config.queue_paused or len(self._active_jobs) >= config.max_concurrent_jobs:
                    await asyncio.sleep(_IDLE_POLL_S)
                    continue
                supported = [c for c in Codec if gpu_cap.supports(c)]
                if not supported:
                    await asyncio.sleep(30.0)
                    continue
                work = await self._claim(supported)
                if work is None:
                    await asyncio.sleep(_IDLE_POLL_S)
                    continue
                await self._process(work, gpu_cap, slot, config)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Worker %s#%d : erreur inattendue", gpu_cap.drm_node, slot)
                await asyncio.sleep(_IDLE_POLL_S)

    async def _claim(self, supported_codecs: list[Codec]) -> _ClaimedWork | None:
        """Claim atomique du prochain fichier éligible (FOR UPDATE SKIP LOCKED)."""
        async with session_scope() as session:
            stmt = (
                select(MediaFile)
                .join(Library, Library.id == MediaFile.library_id)
                .where(
                    MediaFile.status == MediaStatus.QUEUED,
                    Library.enabled.is_(True),
                    Library.paused.is_(False),
                    Library.output_codec.in_(supported_codecs),
                    or_(
                        MediaFile.next_attempt_at.is_(None),
                        MediaFile.next_attempt_at <= _now(),
                    ),
                )
                .order_by(
                    MediaFile.manual_rank.asc().nulls_last(),
                    Library.priority.desc(),
                    MediaFile.estimated_gain_bytes.desc().nulls_last(),
                    MediaFile.mtime.desc(),
                )
                .limit(1)
                .with_for_update(of=MediaFile, skip_locked=True)
            )
            mf = (await session.execute(stmt)).scalars().first()
            if mf is None:
                return None
            forced = mf.manual_rank == 0
            mf.status = MediaStatus.PROCESSING
            mf.manual_rank = None  # consommé (§7.2)
            strategy = parse_strategy(mf.next_strategy)

            # Reprise après crash : réutilise un job remis en queued (§5.2)
            job = (
                (
                    await session.execute(
                        select(Job)
                        .where(Job.media_file_id == mf.id, Job.state == JobState.QUEUED)
                        .order_by(Job.id.desc())
                        .limit(1)
                    )
                )
                .scalars()
                .first()
            )
            if job is None:
                mf.attempt_count += 1
                job = Job(
                    media_file_id=mf.id,
                    library_id=mf.library_id,
                    state=JobState.QUEUED,
                    dry_run=True,  # affiné au démarrage du pipeline
                    rule_hash=mf.applied_rule_hash or "pending",
                )
                session.add(job)
            job.attempt = max(1, mf.attempt_count)
            job.strategy = strategy.value
            await session.flush()
            return _ClaimedWork(
                job_id=job.id,
                media_id=mf.id,
                library_id=mf.library_id,
                attempt=job.attempt,
                strategy=strategy,
                forced=forced,
            )

    # ------------------------------------------------------------------
    # Pipeline d'un job
    # ------------------------------------------------------------------
    async def _process(
        self,
        work: _ClaimedWork,
        gpu_cap: GpuCapability,
        slot: int,
        config: RuntimeConfig,
    ) -> None:
        job_id = work.job_id
        cancel_event = asyncio.Event()
        self._cancel_events[job_id] = cancel_event
        tmp_path: str | None = None
        intermediate_path: str | None = None
        self._active_jobs[job_id] = {
            "job_id": job_id,
            "media_id": work.media_id,
            "library_id": work.library_id,
            "path": "",
            "gpu": gpu_cap.name,
            "drm_node": gpu_cap.drm_node,
            "slot": slot,
            "dry_run": False,
            "started_at": _now().isoformat(),
            "percent": 0.0,
            "phase": "pré-vol",
            "strategy": work.strategy.value,
            "attempt": work.attempt,
        }
        try:
            # --- Préparation + pré-vol -----------------------------------------
            try:
                prep = await self._prepare(work, gpu_cap, slot, config)
            except _PrepareAbort:
                return
            tmp_path = prep.tmp_path
            intermediate_path = prep.intermediate_path
            probe, rule, plan = prep.probe, prep.rule, prep.plan

            self._active_jobs[job_id].update(
                path=probe.path,
                dry_run=prep.dry_run,
                strategy=work.strategy.value,
                phase="encodage",
            )
            logger.info(
                "Job %d : DÉMARRAGE %s — %s → %s %dk (%s, stratégie %s, tentative %d, "
                "worker %s#%d%s)",
                job_id,
                os.path.basename(probe.path),
                probe.video_codec or "?",
                rule.codec.value,
                rule.target_kbps,
                plan.action.value,
                work.strategy.value,
                work.attempt,
                gpu_cap.drm_node,
                slot,
                ", DRY-RUN" if prep.dry_run else "",
            )
            event_bus.publish_queue(
                {"type": "job_started", "job": dict(self._active_jobs[job_id])}
            )

            # --- Encodage ------------------------------------------------------
            last_snapshot = 0.0
            last_progress_log = 0.0
            early_abort_no_gain = False
            src_size_for_abort = (
                os.path.getsize(probe.path) if os.path.exists(probe.path) else 0
            )
            # Seuil §5.5.7 avec 5 % de marge : la projection VBR est bruitée,
            # on n'abandonne que si le dépassement est franc.
            no_gain_threshold = (
                src_size_for_abort * (1.0 - prep.min_gain_percent / 100.0) * 1.05
            )

            async def on_progress(u: ProgressUpdate) -> None:
                nonlocal last_snapshot, last_progress_log, early_abort_no_gain
                # Abandon anticipé (façon Tdarr) : dès 15 % encodés, si la
                # taille PROJETÉE dépasse déjà le seuil de gain, inutile de
                # finir — surtout sur un gros 4K (§5.5.7, early_abort_no_gain)
                if (
                    config.early_abort_no_gain
                    and not work.forced
                    and not early_abort_no_gain
                    and src_size_for_abort > 0
                    and u.percent is not None
                    and u.percent >= 15.0
                    and u.total_size_bytes is not None
                ):
                    projected = u.total_size_bytes / (u.percent / 100.0)
                    if projected > no_gain_threshold:
                        early_abort_no_gain = True
                        logger.info(
                            "Job %d : ABANDON ANTICIPÉ à %.0f%% — taille projetée "
                            "%.2f Gio > seuil %.2f Gio (gain insuffisant)",
                            job_id,
                            u.percent,
                            projected / 1024**3,
                            no_gain_threshold / 1024**3,
                        )
                        self._set_phase(job_id, "abandon anticipé : gain projeté insuffisant")
                        cancel_event.set()
                        return
                now_mono = time.monotonic()
                if now_mono - last_progress_log >= 30.0:
                    last_progress_log = now_mono
                    logger.info(
                        "Job %d : encodage %.1f%% (fps %.0f, x%.2f, ETA %s)",
                        job_id,
                        u.percent or 0.0,
                        u.fps or 0.0,
                        u.speed or 0.0,
                        f"{int((u.eta_s or 0) // 60)}m{int((u.eta_s or 0) % 60):02d}s",
                    )
                info = self._active_jobs.get(job_id)
                if info is not None:
                    info.update(percent=u.percent, fps=u.fps, speed=u.speed, eta_s=u.eta_s)
                progress_event = {
                    "type": "progress",
                    "percent": u.percent,
                    "fps": u.fps,
                    "speed": u.speed,
                    "eta_s": u.eta_s,
                }
                event_bus.publish_queue({**progress_event, "job_id": job_id})
                event_bus.publish_log(job_id, progress_event)
                if now_mono - last_snapshot >= config.progress_snapshot_interval_s:
                    last_snapshot = now_mono
                    await self._save_snapshot(job_id, u)

            async def on_log(line: str) -> None:
                event_bus.publish_log(job_id, {"type": "log", "line": line})

            result = await run_ffmpeg(
                prep.cmd,
                duration_s=probe.duration_s,
                log_path=prep.log_path,
                on_progress=on_progress,
                on_log_line=on_log,
                cancel_event=cancel_event,
            )

            logger.info(
                "Job %d : encodage terminé (code %d, fps moyen %s, vitesse x%s)",
                job_id,
                result.exit_code,
                result.avg_fps,
                result.avg_speed,
            )
            if prep.intermediate_path and (
                early_abort_no_gain or result.cancelled or result.exit_code != 0
            ):
                with contextlib.suppress(OSError):
                    os.unlink(prep.intermediate_path)
            if early_abort_no_gain:
                await self._finalize_early_no_gain(
                    work, rule, prep.tmp_path, result.log_tail, result.exit_code
                )
                return
            if result.cancelled:
                await self._finalize_cancelled(work, result.log_tail, result.exit_code)
                return

            # --- Assemblage mkvmerge (stratégie mkvmerge_mux) --------------------
            if result.exit_code == 0 and prep.mkvmerge_cmd and prep.intermediate_path:
                self._set_phase(job_id, "assemblage mkvmerge (pistes d'origine recopiées)")
                merge_code, merge_output = await run_mkvmerge(prep.mkvmerge_cmd)
                with contextlib.suppress(OSError):
                    os.unlink(prep.intermediate_path)
                with contextlib.suppress(OSError):
                    with open(prep.log_path, "a", encoding="utf-8") as fh:
                        fh.write("# mkvmerge : " + " ".join(prep.mkvmerge_cmd) + "\n")
                        fh.write(merge_output[-8000:] + "\n")
                if merge_code >= 2:
                    with contextlib.suppress(OSError):
                        os.unlink(prep.tmp_path)
                    await self._record_failure(
                        work,
                        Failure(
                            FailureCategory.MUX_OTHER,
                            f"assemblage mkvmerge impossible (code {merge_code})",
                            key_error_lines(merge_output) or merge_output[-1500:],
                        ),
                        config=config,
                        gpu_cap=gpu_cap,
                    )
                    return

            # --- Statistiques de pistes réelles (bitrate exact dans l'UI) ------
            # (mkvmerge les écrit lui-même lors de l'assemblage)
            if (
                result.exit_code == 0
                and prep.mkvmerge_cmd is None
                and prep.container is Container.MKV
                and self.capabilities.mkvpropedit_ok
            ):
                self._set_phase(job_id, "calcul des statistiques de pistes")
                await add_track_statistics_mkv(
                    prep.tmp_path, mkvpropedit_path=config.mkvpropedit_path
                )

            # --- Injection MDCV/CLL conteneur si nécessaire (arbitrage 1) ------
            if (
                result.exit_code == 0
                and plan.output_hdr_format is HdrFormat.HDR10
                and prep.container is Container.MKV
                and self.capabilities.mkvpropedit_ok
            ):
                self._set_phase(job_id, "injection des métadonnées HDR10")
                meta = await resolve_output_hdr10_metadata(
                    probe,
                    ffmpeg_path=config.ffmpeg_path,
                    dovi_tool_path=config.dovi_tool_path,
                    tmp_dir=config.tmp_dir,
                )
                await inject_hdr10_metadata_mkv(
                    prep.tmp_path, meta, mkvpropedit_path=config.mkvpropedit_path
                )

            # --- Validation (§5.5) ----------------------------------------------
            await self._set_job_state(job_id, JobState.VALIDATING)
            self._set_phase(job_id, "validation 1/9")
            event_bus.publish_queue({"type": "job_validating", "job_id": job_id})

            async def on_check(label: str) -> None:
                self._set_phase(job_id, f"validation {label}")

            last_decode_log = 0.0

            async def on_decode_progress(percent: float) -> None:
                nonlocal last_decode_log
                info = self._active_jobs.get(job_id)
                if info is not None:
                    info["percent"] = percent
                event_bus.publish_queue(
                    {
                        "type": "progress",
                        "job_id": job_id,
                        "percent": percent,
                        "fps": None,
                        "speed": None,
                        "eta_s": None,
                    }
                )
                now_mono = time.monotonic()
                if now_mono - last_decode_log >= 30.0:
                    last_decode_log = now_mono
                    logger.info("Job %d : validation — décodage d'intégrité %.0f%%", job_id, percent)

            src_size = os.path.getsize(probe.path) if os.path.exists(probe.path) else 0
            report = await validate_output(
                ffmpeg_path=config.ffmpeg_path,
                ffprobe_path=config.ffprobe_path,
                src_probe=probe,
                src_size_bytes=src_size,
                dst_path=prep.tmp_path,
                exit_code=result.exit_code,
                plan=plan,
                target_codec=rule.codec,
                min_gain_percent=prep.min_gain_percent,
                validation_decode=config.validation_decode,
                dv_ssim_threshold=config.dv_ssim_threshold,
                dv_chroma_delta_max=config.dv_chroma_delta_max,
                tmp_dir=config.tmp_dir,
                stream_plan=prep.stream_plan,
                ffmpeg_log_path=prep.log_path,
                ffmpeg_log_offset=prep.log_offset,
                on_check=on_check,
                on_decode_progress=on_decode_progress,
            )

            self._set_phase(job_id, "finalisation")
            await self._finalize(
                work=work,
                gpu_cap=gpu_cap,
                probe=probe,
                rule=rule,
                report=report,
                run_log_tail=result.log_tail,
                exit_code=result.exit_code,
                avg_fps=result.avg_fps,
                avg_speed=result.avg_speed,
                tmp_path=prep.tmp_path,
                dry_run=prep.dry_run,
                config=config,
                container=prep.container,
            )
        except Exception as exc:
            logger.exception("Job %d : erreur inattendue", job_id)
            await self._record_failure(
                work,
                Failure(FailureCategory.INTERNAL, f"erreur interne : {exc}"),
                config=config,
                gpu_cap=gpu_cap,
            )
            for leftover in (tmp_path, intermediate_path):
                if leftover and os.path.exists(leftover):
                    with contextlib.suppress(OSError):
                        os.unlink(leftover)
        finally:
            self._cancel_events.pop(job_id, None)
            info = self._active_jobs.pop(job_id, None)
            if info is not None:
                event_bus.publish_queue({"type": "job_finished", "job_id": job_id})

    async def _prepare(
        self,
        work: _ClaimedWork,
        gpu_cap: GpuCapability,
        slot: int,
        config: RuntimeConfig,
    ) -> _Prepared:
        """Pré-vol, plan, règle, plan de pistes, commande.

        Lève _PrepareAbort quand l'issue du job a déjà été enregistrée
        (échec classé, politique, gain prévu insuffisant). Les opérations
        longues (sonde, test de décodage) se font hors session DB."""
        report = PreflightReport()
        requested = work.strategy
        strategy = work.strategy

        async def abort(failure: Failure) -> _PrepareAbort:
            await self._record_failure(
                work, failure, config=config, gpu_cap=gpu_cap,
                diagnostics={"preflight": report.as_dict()},
            )
            return _PrepareAbort()

        async with session_scope() as session:
            mf = await session.get(MediaFile, work.media_id)
            job = await session.get(Job, work.job_id)
            library = await session.get(Library, work.library_id)
            if mf is None or job is None:
                raise _PrepareAbort()
            if library is None:
                raise await abort(Failure(FailureCategory.INTERNAL, "bibliothèque supprimée"))
            src_path = mf.path
            lib = _LibrarySnapshot(
                path=library.path,
                container=library.container,
                codec=library.output_codec,
                hdr_policy=library.hdr_policy,
                dv_policy=library.dv_policy,
                min_gain_percent=float(library.min_gain_percent),
                dry_run=library.dry_run,
            )
            rules = list(
                (
                    await session.execute(
                        select(BitrateRule).where(BitrateRule.library_id == library.id)
                    )
                )
                .scalars()
                .all()
            )
            # Détache les règles de la session (lecture seule ensuite)
            rule_rows = [
                _RuleRow(
                    codec=r.codec,
                    resolution_bucket=r.resolution_bucket,
                    is_hdr=r.is_hdr,
                    target_bitrate_kbps=r.target_bitrate_kbps,
                    max_bitrate_kbps=r.max_bitrate_kbps,
                    bufsize_kbps=r.bufsize_kbps,
                )
                for r in rules
            ]
        info = self._active_jobs.get(work.job_id)
        if info is not None:
            info["path"] = src_path
            event_bus.publish_queue({"type": "job_started", "job": dict(info)})

        # --- 1. Source lisible et stable ---------------------------------------
        self._set_phase(work.job_id, "pré-vol : accès à la source")
        access, access_detail = await asyncio.to_thread(check_source_access, src_path)
        report.add("source", "ok" if access == "ok" else "error", access_detail)
        if access == "busy":
            raise await abort(Failure(FailureCategory.SOURCE_BUSY, access_detail))
        if access == "error":
            raise await abort(Failure(FailureCategory.SOURCE_UNREADABLE, access_detail))

        # --- 2. Sonde -------------------------------------------------------------
        try:
            probe = await probe_file(src_path, config.ffprobe_path)
        except ProbeError as exc:
            report.add("sonde", "error", str(exc)[:300])
            raise await abort(
                Failure(FailureCategory.SOURCE_UNREADABLE, f"sonde impossible : {exc}"[:500])
            ) from None
        if probe.resolution_bucket is None or probe.video_codec is None:
            report.add("sonde", "error", "aucune piste vidéo exploitable")
            raise await abort(
                Failure(FailureCategory.SOURCE_UNREADABLE, "résolution vidéo indéterminée")
            )
        report.add(
            "sonde",
            "ok",
            f"{probe.video_codec} {probe.width}x{probe.height}, "
            f"{(probe.duration_s or 0) / 60:.1f} min, {len(probe.audio_streams)} audio, "
            f"{len(probe.subtitle_streams)} sous-titre(s), {probe.format_name or '?'}",
        )

        # --- 3. Plan de traitement (HDR/DV) -----------------------------------------
        plan = plan_treatment(
            probe,
            hdr_policy=lib.hdr_policy,
            dv_policy=lib.dv_policy,
            caps=self.capabilities.dv_caps,
        )
        if plan.action is TreatmentAction.SKIP:
            async with session_scope() as session:
                job_row = await session.get(Job, work.job_id)
                mf_row = await session.get(MediaFile, work.media_id)
                if job_row is not None:
                    job_row.state = JobState.CANCELLED
                    job_row.error_message = plan.reason
                    job_row.strategy = Strategy.PREFLIGHT.value
                    job_row.finished_at = _now()
                if mf_row is not None:
                    mf_row.status = MediaStatus.SKIPPED_POLICY
                    mf_row.last_error = plan.reason
                    _reset_series(mf_row)
            raise _PrepareAbort()

        # --- 4. Règle de bitrate ------------------------------------------------------
        try:
            rule = resolve_rule(
                rule_rows,
                codec=lib.codec,
                bucket=probe.resolution_bucket,
                is_hdr=plan.effective_is_hdr,
                container=lib.container,
                hdr_policy=lib.hdr_policy,
                dv_policy=lib.dv_policy,
            )
        except NoRuleError as exc:
            raise await abort(Failure(FailureCategory.NO_RULE, str(exc))) from None

        # --- 5. Gain atteignable ? ---------------------------------------------------------
        if config.preflight_gain_check and not work.forced:
            duration_for_gain = probe.duration_s
            if probe.video_bitrate_kbps is None:
                # Bitrate estimé depuis taille/durée : la durée conteneur doit
                # être fiable → durée réelle de la piste vidéo (paquets)
                timeline = await probe_stream_timeline(
                    src_path, config.ffprobe_path, duration_hint_s=probe.duration_s,
                    full_scan=is_legacy_timestamp_format(probe.format_name),
                )
                if timeline is not None and timeline.duration_s > 0:
                    duration_for_gain = timeline.duration_s
            prediction = predict_gain(
                src_video_kbps=probe.video_bitrate_kbps,
                size_bytes=os.path.getsize(src_path),
                duration_s=duration_for_gain,
                target_kbps=rule.target_kbps,
                min_gain_percent=lib.min_gain_percent,
            )
            report.gain = prediction
            report.add("gain", "error" if prediction.hopeless else "ok", prediction.reason)
            if prediction.hopeless:
                await self._finalize_preflight_no_gain(work, rule, probe, prediction.reason, report)
                raise _PrepareAbort()
        elif work.forced:
            report.add("gain", "skipped", "réencodage forcé : prédiction de gain ignorée")

        # --- 6. Test de décodage de la source -----------------------------------------------
        if config.preflight_decode_check:
            self._set_phase(work.job_id, "pré-vol : test de décodage de la source")
            healthy, decode_detail = await quick_decode_test(
                config.ffmpeg_path, src_path, probe.duration_s
            )
            report.decode_errors = not healthy
            report.add("décodage", "ok" if healthy else "warning", decode_detail)
            if not healthy and strategy.level < Strategy.TOLERANT.level:
                strategy = Strategy.TOLERANT
                report.add(
                    "stratégie",
                    "warning",
                    "source avec erreurs de décodage : démarrage direct en mode tolérant",
                )

        # --- 7. API matérielle (repli alt_api) ------------------------------------------------
        gpu_api = gpu_cap.effective_api
        if strategy.alternate_api:
            if gpu_cap.alt_api is not None and gpu_cap.supports_alt(rule.codec):
                gpu_api = gpu_cap.alt_api
                report.add("api", "ok", f"API alternative {gpu_api.value} (repli)")
            else:
                strategy = Strategy.SAFE_STREAMS
                report.add(
                    "api", "warning",
                    "API alternative indisponible sur ce GPU : stratégie safe_streams",
                )

        # --- 8. Plan de pistes ------------------------------------------------------------------
        stream_plan = plan_streams(
            probe.raw.get("streams") or [], container=lib.container, strategy=strategy
        )
        adjustments = stream_plan.adjustments
        report.add(
            "pistes",
            "warning" if adjustments else "ok",
            (
                f"{len(adjustments)} ajustement(s) : "
                + " ; ".join(
                    f"#{d.index} {d.codec or d.kind} {d.action.value}"
                    + (f" → {d.target_codec}" if d.target_codec else "")
                    for d in adjustments[:6]
                )
            )
            if adjustments
            else "toutes les pistes sont copiables telles quelles",
        )
        if stream_plan.video is None:
            raise await abort(
                Failure(FailureCategory.SOURCE_UNREADABLE, "aucune piste vidéo à réencoder")
            )

        dry_run = lib.dry_run if lib.dry_run is not None else config.dry_run_global

        # --- 9. Espace disque (§5.6) — estimation prudente : taille source -----------------------
        # Taille ET date relevées maintenant : référence de la garde anti-upgrade.
        # La date enregistrée au dernier scan ne convient pas : un autre service
        # peut retoucher le fichier entre le scan et le job (constaté en prod)
        # et la garde annulait alors à tort un encodage valide.
        src_stat = os.stat(src_path)
        src_size = src_stat.st_size
        try:
            if dry_run:
                os.makedirs(config.dry_run_out_dir, exist_ok=True)
                check_free_space(
                    config.dry_run_out_dir,
                    needed_bytes=src_size,
                    min_free_gb=config.min_free_disk_gb,
                )
                tmp_path = make_dry_run_path(
                    src_path,
                    library_path=lib.path,
                    out_dir=config.dry_run_out_dir,
                    container=lib.container,
                )
            else:
                check_free_space(
                    os.path.dirname(src_path),
                    needed_bytes=src_size,
                    min_free_gb=config.min_free_disk_gb,
                )
                tmp_path = make_tmp_path(
                    src_path,
                    container=lib.container,
                    tmp_strategy=config.tmp_strategy,
                    tmp_dir=config.tmp_dir,
                )
        except DiskSpaceError as exc:
            report.add("disque", "error", str(exc))
            raise await abort(Failure(FailureCategory.DISK_SPACE, str(exc))) from None
        report.add("disque", "ok", "espace libre suffisant")

        # --- 10. Assemblage mkvmerge (repli) ------------------------------------------------------
        ffmpeg_plan = stream_plan
        ffmpeg_dst = tmp_path
        intermediate_path: str | None = None
        mkvmerge_cmd: list[str] | None = None
        if (
            strategy.mkvmerge_assembly
            and lib.container is Container.MKV
            and is_matroska(probe.format_name)
            and self.capabilities.mkvmerge_ok
        ):
            merge_bin = mkvmerge_path_for(config.mkvpropedit_path)
            try:
                assembly = plan_assembly(stream_plan, await identify_tracks(merge_bin, src_path))
            except AssemblyError as exc:
                report.add("assemblage", "warning", f"mkvmerge impossible ({exc}) : muxage FFmpeg")
            else:
                ffmpeg_plan = assembly.ffmpeg_plan
                intermediate_path = intermediate_path_for(tmp_path)
                ffmpeg_dst = intermediate_path
                mkvmerge_cmd = mkvmerge_args(
                    mkvmerge_path=merge_bin,
                    intermediate_path=intermediate_path,
                    source_path=src_path,
                    output_path=tmp_path,
                    plan=assembly,
                )
                report.add(
                    "assemblage",
                    "ok",
                    f"mkvmerge : {len(assembly.source_audio_ids)} audio et "
                    f"{len(assembly.source_subtitle_ids)} sous-titre(s) recopiés bit à bit "
                    "depuis la source",
                )
        elif strategy.mkvmerge_assembly and lib.container is Container.MKV:
            report.add(
                "assemblage",
                "warning",
                "mkvmerge indisponible ou source non Matroska : muxage FFmpeg",
            )

        # --- 11. Commande --------------------------------------------------------------------------
        try:
            cmd = build_ffmpeg_cmd(
                BuildParams(
                    ffmpeg_path=config.ffmpeg_path,
                    src_path=src_path,
                    dst_path=ffmpeg_dst,
                    gpu_api=gpu_api,
                    drm_node=gpu_cap.drm_node,
                    rule=rule,
                    container=lib.container,
                    plan=plan,
                    src_bit_depth=probe.bit_depth,
                    stream_plan=ffmpeg_plan,
                    p5_zero_copy=gpu_cap.p5_zero_copy and gpu_api is gpu_cap.effective_api,
                    strategy=strategy,
                    input_format=probe.format_name,
                )
            )
        except BuildError as exc:
            raise await abort(Failure(FailureCategory.SOURCE_UNREADABLE, str(exc))) from None

        log_path = os.path.join(config.log_dir, f"job_{work.job_id}.log")
        try:
            log_offset = os.path.getsize(log_path)
        except OSError:
            log_offset = 0
        work.strategy = strategy
        diagnostics: dict[str, Any] = {
            "preflight": report.as_dict(),
            "stream_plan": stream_plan.as_dict(),
            "strategy": {
                "used": strategy.value,
                "requested": requested.value,
                "gpu_api": gpu_api.value,
                "attempt": work.attempt,
                "container": lib.container.value,
                "input_format": probe.format_name,
                "mkvmerge": mkvmerge_cmd is not None,
            },
            "treatment": {"action": plan.action.value, "reason": plan.reason},
        }
        if mkvmerge_cmd is not None:
            diagnostics["mkvmerge_cmd"] = mkvmerge_cmd
        _write_log_header(log_path, work, strategy, report, stream_plan, cmd)

        # Snapshot « avant » + passage en running
        async with session_scope() as session:
            job = await session.get(Job, work.job_id)
            mf = await session.get(MediaFile, work.media_id)
            if job is None or mf is None:
                raise _PrepareAbort()
            job.state = JobState.RUNNING
            job.dry_run = dry_run
            job.rule_hash = rule.rule_hash
            job.gpu_id = gpu_cap.gpu_id
            job.worker_slot = slot
            job.started_at = _now()
            job.strategy = strategy.value
            job.src_path = src_path
            job.src_size_bytes = src_size
            job.src_codec = probe.video_codec
            job.src_bitrate_kbps = probe.video_bitrate_kbps
            job.src_resolution = f"{probe.width}x{probe.height}" if probe.width else None
            job.src_hdr_format = probe.hdr_format
            job.src_duration_s = (
                decimal.Decimal(f"{probe.duration_s:.3f}") if probe.duration_s else None
            )
            job.src_mtime = datetime.datetime.fromtimestamp(
                src_stat.st_mtime, tz=datetime.timezone.utc
            )
            job.dst_path = tmp_path
            job.ffmpeg_cmd = cmd
            job.log_path = log_path
            job.diagnostics = diagnostics

        if adjustments:
            logger.info(
                "Job %d : plan de pistes — %s",
                work.job_id,
                " ; ".join(f"#{d.index} {d.action.value} ({d.reason})" for d in adjustments),
            )
        return _Prepared(
            probe=probe,
            rule=rule,
            plan=plan,
            stream_plan=stream_plan,
            container=lib.container,
            min_gain_percent=lib.min_gain_percent,
            cmd=cmd,
            tmp_path=tmp_path,
            dry_run=dry_run,
            log_path=log_path,
            gpu_api=gpu_api,
            diagnostics=diagnostics,
            log_offset=log_offset,
            intermediate_path=intermediate_path,
            mkvmerge_cmd=mkvmerge_cmd,
        )

    # ------------------------------------------------------------------
    # Finalisations
    # ------------------------------------------------------------------
    async def _save_snapshot(self, job_id: int, u: ProgressUpdate) -> None:
        with contextlib.suppress(Exception):
            async with session_scope() as session:
                session.add(
                    JobProgressSnapshot(
                        job_id=job_id,
                        percent=decimal.Decimal(f"{u.percent:.2f}") if u.percent else None,
                        fps=decimal.Decimal(f"{u.fps:.2f}") if u.fps else None,
                        speed=decimal.Decimal(f"{u.speed:.2f}") if u.speed else None,
                        bitrate_kbps=int(u.bitrate_kbps) if u.bitrate_kbps else None,
                    )
                )

    async def _set_job_state(self, job_id: int, state: JobState) -> None:
        async with session_scope() as session:
            await session.execute(update(Job).where(Job.id == job_id).values(state=state))

    def _apply_failure(
        self,
        job: Job | None,
        mf: MediaFile | None,
        work: _ClaimedWork,
        failure: Failure,
        *,
        config: RuntimeConfig,
        gpu_cap: GpuCapability | None,
        rule_hash: str | None = None,
    ) -> FailureDecision | None:
        """Enregistre un échec sur le job et décide de la suite pour le fichier."""
        now = _now()
        decision: FailureDecision | None = None
        if mf is not None:
            tried = list(dict.fromkeys([*(mf.strategies_tried or []), work.strategy.value]))
            codec = _target_codec(job) if job is not None else None
            alt_available = bool(
                gpu_cap is not None
                and gpu_cap.alt_api is not None
                and (codec is None or gpu_cap.supports_alt(codec))
            )
            decision = decide_after_failure(
                failure.category,
                tried=tried,
                retry_count=mf.retry_count,
                attempt_count=mf.attempt_count,
                current=work.strategy,
                max_retries=config.max_retries,
                auto_fallback=config.auto_fallback,
                alt_api_available=alt_available,
                changes_command=_command_change_predicate(
                    job, tried, mkvmerge_available=self.capabilities.mkvmerge_ok
                ),
            )
            mf.strategies_tried = tried
            mf.failure_category = failure.category.value
            mf.last_error = failure.message
            # Hash de la règle de la tentative : le scan ne relance plus un
            # échec définitif tant que la règle (ou le fichier) ne change pas
            if rule_hash:
                mf.applied_rule_hash = rule_hash
            if decision.kind == "retry":
                mf.retry_count += 1
                mf.status = MediaStatus.QUEUED
                mf.next_strategy = work.strategy.value
                mf.next_attempt_at = now + datetime.timedelta(seconds=decision.retry_in_s or 0)
            elif decision.kind == "fallback":
                mf.status = MediaStatus.QUEUED
                mf.next_strategy = decision.next_strategy.value if decision.next_strategy else None
                mf.next_attempt_at = None
            else:
                mf.status = MediaStatus.FAILED
                mf.next_strategy = None
                mf.next_attempt_at = None
        if job is not None:
            job.state = JobState.FAILED
            job.error_message = failure.message
            job.failure_category = failure.category.value
            job.failure_detail = failure.detail
            job.finished_at = now
            if job.started_at is not None:
                job.duration_s = decimal.Decimal(
                    f"{(now - job.started_at).total_seconds():.3f}"
                )
            if decision is not None:
                job.diagnostics = {**(job.diagnostics or {}), "decision": decision.as_dict()}
        logger.warning(
            "Job %d : ÉCHEC [%s] %s → %s",
            work.job_id,
            failure.category.value,
            failure.message,
            f"{decision.kind} ({decision.reason})" if decision else "—",
        )
        event_bus.publish_queue(
            {
                "type": "job_failed",
                "job_id": work.job_id,
                "error": failure.message,
                "category": failure.category.value,
                "decision": decision.as_dict() if decision else None,
            }
        )
        return decision

    async def _record_failure(
        self,
        work: _ClaimedWork,
        failure: Failure,
        *,
        config: RuntimeConfig,
        gpu_cap: GpuCapability | None,
        diagnostics: dict[str, Any] | None = None,
    ) -> None:
        async with session_scope() as session:
            job = await session.get(Job, work.job_id)
            mf = await session.get(MediaFile, work.media_id)
            if job is not None and diagnostics:
                job.diagnostics = {**(job.diagnostics or {}), **diagnostics}
                if job.strategy is None:
                    job.strategy = work.strategy.value
            self._apply_failure(
                job, mf, work, failure, config=config, gpu_cap=gpu_cap,
                rule_hash=job.rule_hash if job is not None and job.rule_hash != "pending" else None,
            )

    async def _finalize_preflight_no_gain(
        self,
        work: _ClaimedWork,
        rule: ResolvedRule,
        probe: MediaProbe,
        reason: str,
        report: PreflightReport,
    ) -> None:
        """Gain prévu insuffisant : issue « sans gain » SANS encoder."""
        async with session_scope() as session:
            job = await session.get(Job, work.job_id)
            mf = await session.get(MediaFile, work.media_id)
            if job is not None:
                job.state = JobState.SKIPPED_NO_GAIN
                job.strategy = Strategy.PREFLIGHT.value
                job.rule_hash = rule.rule_hash
                job.error_message = f"gain prévu insuffisant : {reason}"
                job.src_path = probe.path
                job.src_codec = probe.video_codec
                job.src_bitrate_kbps = probe.video_bitrate_kbps
                job.src_resolution = f"{probe.width}x{probe.height}" if probe.width else None
                job.src_hdr_format = probe.hdr_format
                job.src_size_bytes = mf.size_bytes if mf is not None else None
                job.started_at = job.started_at or _now()
                job.finished_at = _now()
                job.duration_s = decimal.Decimal("0")
                job.diagnostics = {"preflight": report.as_dict()}
            if mf is not None:
                mf.status = MediaStatus.SKIPPED_NO_GAIN
                mf.applied_rule_hash = rule.rule_hash
                mf.last_error = f"gain prévu insuffisant : {reason}"
                _reset_series(mf)
        logger.info("Job %d : SANS GAIN (prévu avant encodage) — %s", work.job_id, reason)
        event_bus.publish_queue({"type": "job_no_gain", "job_id": work.job_id, "preflight": True})

    async def _finalize_early_no_gain(
        self,
        work: _ClaimedWork,
        rule: ResolvedRule,
        tmp_path: str,
        log_tail: str,
        exit_code: int,
    ) -> None:
        """Encodage interrompu car la projection de taille dépassait le seuil
        de gain : même issue que le contrôle 7 (§5.5.7) — original conservé,
        fichier marqué traité pour ne pas être retenté, temporaire supprimé."""
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)
        async with session_scope() as session:
            job = await session.get(Job, work.job_id)
            mf = await session.get(MediaFile, work.media_id)
            if job is not None:
                job.state = JobState.SKIPPED_NO_GAIN
                job.exit_code = exit_code
                job.error_message = (
                    "abandon anticipé : taille projetée au-dessus du seuil de gain"
                )
                job.log_tail = log_tail
                job.dst_path = None
                job.finished_at = _now()
                if job.started_at is not None:
                    job.duration_s = decimal.Decimal(
                        f"{(job.finished_at - job.started_at).total_seconds():.3f}"
                    )
            if mf is not None:
                mf.status = MediaStatus.SKIPPED_NO_GAIN
                mf.applied_rule_hash = rule.rule_hash
                mf.last_error = None
                _reset_series(mf)
        event_bus.publish_queue(
            {"type": "job_no_gain", "job_id": work.job_id, "early": True}
        )

    async def _finalize_cancelled(
        self, work: _ClaimedWork, log_tail: str, exit_code: int
    ) -> None:
        async with session_scope() as session:
            job = await session.get(Job, work.job_id)
            mf = await session.get(MediaFile, work.media_id)
            if job is not None:
                job.state = JobState.CANCELLED
                job.exit_code = exit_code
                job.error_message = "annulé par l'utilisateur"
                job.log_tail = log_tail
                job.finished_at = _now()
                if job.dst_path and os.path.exists(job.dst_path) and job.dst_path != job.src_path:
                    with contextlib.suppress(OSError):
                        os.unlink(job.dst_path)
            if mf is not None:
                mf.status = MediaStatus.PENDING
                # Annulation volontaire : ne compte pas comme une tentative
                mf.attempt_count = max(0, mf.attempt_count - 1)
        event_bus.publish_queue({"type": "job_cancelled", "job_id": work.job_id})

    async def _finalize(
        self,
        *,
        work: _ClaimedWork,
        gpu_cap: GpuCapability,
        probe: MediaProbe,
        rule: ResolvedRule,
        report: ValidationReport,
        run_log_tail: str,
        exit_code: int,
        avg_fps: float | None,
        avg_speed: float | None,
        tmp_path: str,
        dry_run: bool,
        config: RuntimeConfig,
        container: Container,
    ) -> None:
        """Applique l'issue de la validation : succès/remplacement, no-gain, échec."""
        dst_probe: MediaProbe | None = report.dst_probe
        dst_size = os.path.getsize(tmp_path) if os.path.exists(tmp_path) else None

        async with session_scope() as session:
            job = await session.get(Job, work.job_id)
            mf = await session.get(MediaFile, work.media_id)
            if job is None or mf is None:
                return
            job.exit_code = exit_code
            job.log_tail = run_log_tail
            job.avg_fps = decimal.Decimal(f"{avg_fps:.2f}") if avg_fps else None
            job.avg_speed = decimal.Decimal(f"{avg_speed:.2f}") if avg_speed else None
            job.validation_report = report.as_dict()
            job.finished_at = _now()
            if job.started_at is not None:
                job.duration_s = decimal.Decimal(
                    f"{(job.finished_at - job.started_at).total_seconds():.3f}"
                )
            if dst_probe is not None:
                job.dst_codec = dst_probe.video_codec
                job.dst_bitrate_kbps = dst_probe.video_bitrate_kbps
                job.dst_resolution = (
                    f"{dst_probe.width}x{dst_probe.height}" if dst_probe.width else None
                )
                job.dst_hdr_format = dst_probe.hdr_format
                job.dst_duration_s = (
                    decimal.Decimal(f"{dst_probe.duration_s:.3f}")
                    if dst_probe.duration_s
                    else None
                )
            job.dst_size_bytes = dst_size

            outcome: ValidationOutcome = report.outcome

            logger.info(
                "Job %d : validation → %s (gain %.1f%%)",
                job.id,
                outcome.value,
                report.gain_percent or 0.0,
            )
            if outcome is ValidationOutcome.NO_GAIN:
                # §5.5.7 : original conservé, fichier marqué traité
                with contextlib.suppress(OSError):
                    os.unlink(tmp_path)
                job.state = JobState.SKIPPED_NO_GAIN
                job.dst_path = None
                mf.status = MediaStatus.SKIPPED_NO_GAIN
                mf.applied_rule_hash = rule.rule_hash
                mf.last_error = None
                _reset_series(mf)
                event_bus.publish_queue(
                    {"type": "job_no_gain", "job_id": work.job_id, "gain": report.gain_percent}
                )
                return

            if outcome is ValidationOutcome.FAILED:
                if not dry_run or os.path.dirname(tmp_path) != config.dry_run_out_dir:
                    with contextlib.suppress(OSError):
                        os.unlink(tmp_path)
                failed = report.failed_check
                failure = classify_validation_failure(
                    failed.name if failed else "unknown",
                    failed.detail if failed else "validation échouée",
                    exit_code=exit_code,
                    ffmpeg_log=run_log_tail,
                    check_meta=failed.meta if failed else None,
                )
                job.dst_path = None
                self._apply_failure(
                    job, mf, work, failure, config=config, gpu_cap=gpu_cap,
                    rule_hash=rule.rule_hash,
                )
                return

            # --- PASSED -------------------------------------------------------
            if job.src_size_bytes and dst_size:
                job.bytes_saved = job.src_size_bytes - dst_size
                job.gain_percent = decimal.Decimal(f"{report.gain_percent:.2f}")

            if dry_run:
                # §9.4 : sortie conservée dans dry_run_out_dir, AUCUN remplacement
                logger.info(
                    "Job %d : SUCCÈS DRY-RUN — sortie conservée dans %s (gain %.1f%%)",
                    job.id,
                    tmp_path,
                    report.gain_percent or 0.0,
                )
                job.state = JobState.SUCCESS
                job.dst_path = tmp_path
                mf.status = MediaStatus.DONE
                mf.applied_rule_hash = rule.rule_hash
                mf.processed_tool_version = TOOL_ID
                mf.last_error = None
                _reset_series(mf)
                event_bus.publish_queue(
                    {
                        "type": "job_success",
                        "job_id": work.job_id,
                        "dry_run": True,
                        "gain": report.gain_percent,
                        "attempt": work.attempt,
                        "strategy": work.strategy.value,
                    }
                )
                return

            # Garde anti-upgrade : si Radarr/Sonarr a REMPLACÉ la source
            # pendant l'encodage (fichier upgradé, même nom), remplacer
            # écraserait la nouvelle version par l'encodage de l'ancienne.
            # On vérifie que la source est identique à ce qui a été sondé.
            try:
                current = os.stat(mf.path)
                src_changed = current.st_size != job.src_size_bytes or (
                    job.src_mtime is not None
                    and abs(current.st_mtime - job.src_mtime.timestamp()) >= 1.0
                )
            except OSError:
                src_changed = True  # source disparue : ne surtout pas écrire
            if src_changed:
                with contextlib.suppress(OSError):
                    os.unlink(tmp_path)
                reason = (
                    "source modifiée pendant l'encodage (upgrade *arr probable) : "
                    "remplacement annulé, le nouveau fichier sera re-traité au scan"
                )
                logger.warning("Job %d : %s", job.id, reason)
                job.state = JobState.CANCELLED
                job.error_message = reason
                job.dst_path = None
                # pending : le prochain scan re-sonde le NOUVEAU fichier et
                # décide (pas de retry compté, ce n'est pas un échec)
                mf.status = MediaStatus.PENDING
                _reset_series(mf)
                event_bus.publish_queue(
                    {"type": "job_cancelled", "job_id": work.job_id, "reason": reason}
                )
                return

            # Remplacement atomique (§5.6) — après les 9 validations UNIQUEMENT
            self._set_phase(job.id, "remplacement atomique")
            job.state = JobState.REPLACING
            try:
                replace_result = await asyncio.to_thread(
                    replace_original,
                    src_path=mf.path,
                    tmp_path=tmp_path,
                    job_id=job.id,
                    trash_dir=config.trash_dir,
                    immediate_delete=config.immediate_delete,
                    container=container,
                )
            except ReplaceError as exc:
                category = (
                    FailureCategory.PERMISSION
                    if "ermission" in str(exc)
                    else FailureCategory.REPLACE_ERROR
                )
                self._apply_failure(
                    job, mf, work,
                    Failure(category, f"remplacement impossible : {exc}"),
                    config=config, gpu_cap=gpu_cap, rule_hash=rule.rule_hash,
                )
                return

            job.state = JobState.SUCCESS
            job.dst_path = replace_result.final_path
            job.trashed_path = replace_result.trashed_path
            logger.info(
                "Job %d : SUCCÈS — %s remplacé (gain %s, stratégie %s, tentative %d, "
                "original en corbeille : %s)",
                job.id,
                os.path.basename(replace_result.final_path),
                f"{report.gain_percent:.1f}%" if report.gain_percent is not None else "?",
                work.strategy.value,
                work.attempt,
                replace_result.trashed_path,
            )

            if replace_result.extension_changed:
                logger.info(
                    "Extension changée : %s → %s (Jellyfin devra rescanner)",
                    mf.path,
                    replace_result.final_path,
                )
            mf.path = replace_result.final_path
            mf.status = MediaStatus.DONE
            mf.applied_rule_hash = rule.rule_hash
            mf.processed_tool_version = TOOL_ID
            mf.transcoder_tag = build_transcoder_tag(rule)
            mf.last_error = None
            _reset_series(mf)
            mf.size_bytes = dst_size
            if dst_probe is not None:
                mf.video_codec = dst_probe.video_codec
                mf.video_bitrate_kbps = dst_probe.video_bitrate_kbps
                mf.pix_fmt = dst_probe.pix_fmt
                mf.bit_depth = dst_probe.bit_depth
                mf.hdr_format = dst_probe.hdr_format
                mf.dv_profile = None
                mf.dv_bl_compat_id = None
                mf.dv_has_el = None
                mf.audio_streams = dst_probe.audio_streams
                mf.subtitle_streams = dst_probe.subtitle_streams
            event_bus.publish_queue(
                {
                    "type": "job_success",
                    "job_id": work.job_id,
                    "dry_run": False,
                    "gain": report.gain_percent,
                    "attempt": work.attempt,
                    "strategy": work.strategy.value,
                }
            )


@dataclass(frozen=True, slots=True)
class _LibrarySnapshot:
    """Réglages de la bibliothèque lus en début de préparation (hors session)."""

    path: str
    container: Container
    codec: Codec
    hdr_policy: HdrPolicy
    dv_policy: DvPolicy
    min_gain_percent: float
    dry_run: bool | None


@dataclass(slots=True)
class _RuleRow:
    """Copie détachée d'une BitrateRule (satisfait RuleLike)."""

    codec: Codec
    resolution_bucket: ResolutionBucket
    is_hdr: bool
    target_bitrate_kbps: int
    max_bitrate_kbps: int
    bufsize_kbps: int


def _command_change_predicate(
    job: Job | None, tried: list[str], *, mkvmerge_available: bool = True
) -> Callable[[Strategy], bool] | None:
    """Prédicat « cette stratégie changerait-elle la commande ? » à partir
    du plan de pistes enregistré par la tentative (None = pas d'avis)."""
    diagnostics = (job.diagnostics or {}) if job is not None else {}
    streams = streams_from_diagnostics(diagnostics.get("stream_plan"))
    strategy_info = diagnostics.get("strategy") or {}
    container_value = strategy_info.get("container")
    if not streams or container_value is None:
        return None
    try:
        container = Container(container_value)
    except ValueError:
        return None
    assembly_possible = (
        mkvmerge_available
        and container is Container.MKV
        and is_matroska(strategy_info.get("input_format"))
    )
    previous = [parse_strategy(t) for t in tried]

    def fingerprint(strategy: Strategy) -> tuple[object, ...]:
        return (
            strategy.tolerant_input,
            strategy.alternate_api,
            strategy.mkvmerge_assembly and assembly_possible,
            plan_signature(streams, container=container, strategy=strategy),
        )

    def changes(candidate: Strategy) -> bool:
        target = fingerprint(candidate)
        return all(fingerprint(done) != target for done in previous)

    return changes


def _target_codec(job: Job) -> Codec | None:
    """Codec cible de la tentative, lu dans la commande enregistrée."""
    for arg in job.ffmpeg_cmd or []:
        if arg.startswith("hevc_"):
            return Codec.HEVC
        if arg.startswith("av1_"):
            return Codec.AV1
    return None


def _write_log_header(
    log_path: str,
    work: _ClaimedWork,
    strategy: Strategy,
    report: PreflightReport,
    stream_plan: StreamPlan,
    cmd: list[str],
) -> None:
    """En-tête lisible en tête du log du job : contexte de la tentative."""
    with contextlib.suppress(OSError):
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(f"# Transcodarr — tentative {work.attempt}, stratégie « {strategy.value} »\n")
            for check in report.checks:
                fh.write(f"# pré-vol [{check.status}] {check.name} : {check.detail}\n")
            for d in stream_plan.decisions:
                if d.action is not StreamAction.COPY:
                    extra = f" → {d.target_codec}" if d.target_codec else ""
                    fh.write(
                        f"# piste #{d.index} {d.kind} {d.codec or '?'} : {d.action.value}{extra}"
                        + (f" ({d.reason})" if d.reason else "")
                        + "\n"
                    )
            fh.write("# commande : " + " ".join(cmd) + "\n")


async def recover_stale_jobs(session: AsyncSession, active_job_ids: set[int]) -> int:
    """Relance les jobs figés en running/validating/replacing qui ne sont
    PAS réellement en cours dans ce process (restes d'un arrêt brutal dont
    la reprise au boot n'a pas suffi, ou état incohérent). Déclenché par
    POST /queue/recover — ne touche jamais un job actif."""
    stale_states = (JobState.RUNNING, JobState.VALIDATING, JobState.REPLACING)
    stale_jobs = (
        (await session.execute(select(Job).where(Job.state.in_(stale_states))))
        .scalars()
        .all()
    )
    recovered = 0
    for job in stale_jobs:
        if job.id in active_job_ids:
            continue
        previous = job.state
        job.state = JobState.QUEUED
        job.worker_slot = None
        mf = await session.get(MediaFile, job.media_file_id)
        if mf is not None and mf.status in (MediaStatus.PROCESSING, MediaStatus.FAILED):
            mf.status = MediaStatus.QUEUED
            mf.next_attempt_at = None
            mf.last_error = None
        recovered += 1
        logger.info("Job %d : figé en %s → remis en queue", job.id, previous.value)
    return recovered


async def requeue_dry_run_files(session: AsyncSession, library_id: int | None = None) -> int:
    """Re-queue les fichiers marqués done dont le DERNIER job réussi était
    un dry-run : après passage en réel, permet de relancer sans attendre le
    scan (le tag conteneur est absent de l'original de toute façon)."""
    stmt = select(MediaFile).where(MediaFile.status == MediaStatus.DONE)
    if library_id is not None:
        stmt = stmt.where(MediaFile.library_id == library_id)
    candidates = (await session.execute(stmt)).scalars().all()
    requeued = 0
    for mf in candidates:
        last_job = (
            (
                await session.execute(
                    select(Job)
                    .where(Job.media_file_id == mf.id, Job.state == JobState.SUCCESS)
                    .order_by(Job.id.desc())
                    .limit(1)
                )
            )
            .scalars()
            .first()
        )
        if last_job is None or not last_job.dry_run:
            continue
        mf.status = MediaStatus.QUEUED
        mf.applied_rule_hash = None
        _reset_series(mf)
        requeued += 1
    logger.info(
        "Re-queue dry-run%s : %d fichier(s)",
        f" (bibliothèque {library_id})" if library_id else "",
        requeued,
    )
    return requeued


def reset_for_retry(mf: MediaFile) -> None:
    """Relance manuelle : nouvelle série complète, depuis la stratégie standard."""
    mf.status = MediaStatus.QUEUED
    mf.last_error = None
    mf.applied_rule_hash = None
    _reset_series(mf)


async def retry_failed_files(
    session: AsyncSession,
    *,
    library_id: int | None = None,
    failure_category: str | None = None,
    file_ids: list[int] | None = None,
) -> int:
    """Remet en file les fichiers en échec définitif (filtrables)."""
    stmt = select(MediaFile).where(MediaFile.status == MediaStatus.FAILED)
    if library_id is not None:
        stmt = stmt.where(MediaFile.library_id == library_id)
    if failure_category:
        stmt = stmt.where(MediaFile.failure_category == failure_category)
    if file_ids:
        stmt = stmt.where(MediaFile.id.in_(file_ids))
    files = (await session.execute(stmt)).scalars().all()
    for mf in files:
        reset_for_retry(mf)
    logger.info(
        "Relance des échecs (bibliothèque=%s, catégorie=%s, sélection=%s) : %d fichier(s)",
        library_id,
        failure_category,
        len(file_ids) if file_ids else None,
        len(files),
    )
    return len(files)


# ---------------------------------------------------------------------------
# Reprise après crash (§5.2)
# ---------------------------------------------------------------------------
async def recover_after_crash(session: AsyncSession) -> int:
    """running/validating/replacing → queued ; fichiers processing → queued."""
    stale_states = (JobState.RUNNING, JobState.VALIDATING, JobState.REPLACING)
    stale_jobs = (
        (await session.execute(select(Job).where(Job.state.in_(stale_states))))
        .scalars()
        .all()
    )
    for job in stale_jobs:
        job.state = JobState.QUEUED
        job.worker_slot = None
    await session.execute(
        update(MediaFile)
        .where(MediaFile.status == MediaStatus.PROCESSING)
        .values(status=MediaStatus.QUEUED)
    )
    if stale_jobs:
        logger.warning(
            "Reprise après crash : %d job(s) remis en queue", len(stale_jobs)
        )
    return len(stale_jobs)


async def cleanup_orphan_tmp(session: AsyncSession, config: RuntimeConfig) -> int:
    """Purge des temporaires orphelins au démarrage (§5.2)."""
    paths = (
        (await session.execute(select(Library.path).where(Library.enabled.is_(True))))
        .scalars()
        .all()
    )
    return await asyncio.to_thread(purge_orphan_tmp, list(paths), config.tmp_dir)


# ---------------------------------------------------------------------------
# Reclassement de l'historique (au démarrage, en tâche de fond)
# ---------------------------------------------------------------------------
async def backfill_failure_categories(batch_size: int = 1000) -> int:
    """Classe les échecs enregistrés avant la classification (0.9.x).

    Parcours par lots (clé primaire décroissante), sans bloquer le démarrage.
    Les fichiers en échec reçoivent la catégorie de leur dernier job."""
    classified = 0
    last_id: int | None = None
    while True:
        async with session_scope() as session:
            stmt = (
                select(Job.id, Job.error_message, Job.log_tail, Job.exit_code)
                .where(Job.state == JobState.FAILED, Job.failure_category.is_(None))
                .order_by(Job.id.desc())
                .limit(batch_size)
            )
            if last_id is not None:
                stmt = stmt.where(Job.id < last_id)
            rows = (await session.execute(stmt)).all()
            if not rows:
                break
            for job_id, message, log_tail, exit_code in rows:
                failure = classify_legacy(message, log_tail, exit_code)
                await session.execute(
                    update(Job)
                    .where(Job.id == job_id)
                    .values(
                        failure_category=failure.category.value,
                        failure_detail=failure.detail,
                        strategy=Strategy.STANDARD.value,
                    )
                )
            last_id = rows[-1][0]
            classified += len(rows)
        await asyncio.sleep(0)

    async with session_scope() as session:
        latest = (
            select(Job.media_file_id, func.max(Job.id).label("job_id"))
            .where(Job.state == JobState.FAILED)
            .group_by(Job.media_file_id)
            .subquery()
        )
        file_rows = (
            await session.execute(
                select(MediaFile, Job.failure_category)
                .join(latest, latest.c.media_file_id == MediaFile.id)
                .join(Job, Job.id == latest.c.job_id)
                .where(MediaFile.status == MediaStatus.FAILED, MediaFile.failure_category.is_(None))
            )
        ).all()
        for mf, category in file_rows:
            mf.failure_category = category
        # Échecs du scanner (sonde, règle) sans job associé
        orphans = (
            await session.execute(
                select(MediaFile).where(
                    MediaFile.status == MediaStatus.FAILED,
                    MediaFile.failure_category.is_(None),
                )
            )
        ).scalars().all()
        for mf in orphans:
            mf.failure_category = classify_legacy(mf.last_error, None, None).category.value
    if classified:
        logger.info("Historique : %d échec(s) reclassé(s) par catégorie", classified)
    return classified
