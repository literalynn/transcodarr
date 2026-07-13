"""Queue et workers asyncio (§5.2) + pipeline complet d'un job.

- slots = Σ worker_count des GPU activés, plafonnés par max_concurrent_jobs ;
- claim par `SELECT … FOR UPDATE SKIP LOCKED` (résistant au redémarrage) ;
- ordonnancement : manual_rank (drag §7.2), puis priority DESC, puis gain
  potentiel estimé, puis mtime DESC — le filtre de date est appliqué au scan ;
- pause globale (app_settings.queue_paused) et par bibliothèque (paused) ;
- retries avec backoff exponentiel, max configurables ;
- reprise après crash : running/validating/replacing → queued + purge tmp.

Pipeline d'un job (§5.3→§5.6) : sonde fraîche → plan DV → règle → commande
→ encodage (progression WS + snapshots) → injection MDCV/CLL conteneur si
nécessaire → les 9 validations → remplacement atomique (ou sortie dry-run).
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import decimal
import logging
import os
import time
from dataclasses import dataclass
from typing import Any

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from transcoder import TOOL_ID
from transcoder.core.capabilities import Capabilities, GpuCapability
from transcoder.core.cmd_builder import BuildError, BuildParams, build_ffmpeg_cmd
from transcoder.core.dovi import (
    TreatmentAction,
    plan_treatment,
    resolve_output_hdr10_metadata,
)
from transcoder.core.probe import MediaProbe, ProbeError, probe_file
from transcoder.core.replace import (
    DiskSpaceError,
    ReplaceError,
    check_free_space,
    inject_hdr10_metadata_mkv,
    make_dry_run_path,
    make_tmp_path,
    purge_orphan_tmp,
    replace_original,
)
from transcoder.core.rules import NoRuleError, ResolvedRule, build_transcoder_tag, resolve_rule
from transcoder.core.runner import ProgressUpdate, run_ffmpeg
from transcoder.core.validate import ValidationOutcome, validate_output
from transcoder.db import session_scope
from transcoder.enums import Codec, Container, HdrFormat, JobState, MediaStatus
from transcoder.events import event_bus
from transcoder.models import BitrateRule, Job, JobProgressSnapshot, Library, MediaFile
from transcoder.runtime_config import RuntimeConfig, load_runtime_config

logger = logging.getLogger(__name__)

_IDLE_POLL_S = 5.0


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
            mf.status = MediaStatus.PROCESSING
            mf.manual_rank = None  # consommé (§7.2)

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
                job = Job(
                    media_file_id=mf.id,
                    library_id=mf.library_id,
                    state=JobState.QUEUED,
                    dry_run=True,  # affiné au démarrage du pipeline
                    rule_hash=mf.applied_rule_hash or "pending",
                )
                session.add(job)
            await session.flush()
            return _ClaimedWork(job_id=job.id, media_id=mf.id, library_id=mf.library_id)

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
        try:
            # --- Préparation (session courte) --------------------------------
            async with session_scope() as session:
                prep = await self._prepare(session, work, gpu_cap, slot, config)
            if prep is None:
                return
            (probe, rule, plan, library_snapshot, cmd, tmp_path, dry_run, log_path) = prep

            self._active_jobs[job_id] = {
                "job_id": job_id,
                "media_id": work.media_id,
                "library_id": work.library_id,
                "path": probe.path,
                "gpu": gpu_cap.name,
                "drm_node": gpu_cap.drm_node,
                "slot": slot,
                "dry_run": dry_run,
                "started_at": _now().isoformat(),
                "percent": 0.0,
            }
            event_bus.publish_queue(
                {"type": "job_started", "job": dict(self._active_jobs[job_id])}
            )

            # --- Encodage ------------------------------------------------------
            last_snapshot = 0.0

            async def on_progress(u: ProgressUpdate) -> None:
                nonlocal last_snapshot
                info = self._active_jobs.get(job_id)
                if info is not None:
                    info.update(
                        percent=u.percent,
                        fps=u.fps,
                        speed=u.speed,
                        eta_s=u.eta_s,
                    )
                event_bus.publish_queue(
                    {
                        "type": "progress",
                        "job_id": job_id,
                        "percent": u.percent,
                        "fps": u.fps,
                        "speed": u.speed,
                        "eta_s": u.eta_s,
                    }
                )
                event_bus.publish_log(
                    job_id,
                    {
                        "type": "progress",
                        "percent": u.percent,
                        "fps": u.fps,
                        "speed": u.speed,
                        "eta_s": u.eta_s,
                    },
                )
                now = time.monotonic()
                if now - last_snapshot >= config.progress_snapshot_interval_s:
                    last_snapshot = now
                    await self._save_snapshot(job_id, u)

            async def on_log(line: str) -> None:
                event_bus.publish_log(job_id, {"type": "log", "line": line})

            result = await run_ffmpeg(
                cmd,
                duration_s=probe.duration_s,
                log_path=log_path,
                on_progress=on_progress,
                on_log_line=on_log,
                cancel_event=cancel_event,
            )

            if result.cancelled:
                await self._finalize_cancelled(work, result.log_tail, result.exit_code)
                return

            # --- Injection MDCV/CLL conteneur si nécessaire (arbitrage 1) ------
            if (
                result.exit_code == 0
                and plan.output_hdr_format is HdrFormat.HDR10
                and library_snapshot["container"] is Container.MKV
                and self.capabilities.mkvpropedit_ok
            ):
                meta = await resolve_output_hdr10_metadata(
                    probe,
                    ffmpeg_path=config.ffmpeg_path,
                    dovi_tool_path=config.dovi_tool_path,
                    tmp_dir=config.tmp_dir,
                )
                await inject_hdr10_metadata_mkv(
                    tmp_path, meta, mkvpropedit_path=config.mkvpropedit_path
                )

            # --- Validation (§5.5) ----------------------------------------------
            await self._set_job_state(job_id, JobState.VALIDATING)
            event_bus.publish_queue({"type": "job_validating", "job_id": job_id})
            src_size = os.path.getsize(probe.path) if os.path.exists(probe.path) else 0
            report = await validate_output(
                ffmpeg_path=config.ffmpeg_path,
                ffprobe_path=config.ffprobe_path,
                src_probe=probe,
                src_size_bytes=src_size,
                dst_path=tmp_path,
                exit_code=result.exit_code,
                plan=plan,
                target_codec=rule.codec,
                min_gain_percent=library_snapshot["min_gain_percent"],
                validation_decode=config.validation_decode,
                dv_ssim_threshold=config.dv_ssim_threshold,
                dv_chroma_delta_max=config.dv_chroma_delta_max,
                tmp_dir=config.tmp_dir,
            )

            await self._finalize(
                work=work,
                probe=probe,
                rule=rule,
                report=report,
                run_log_tail=result.log_tail,
                exit_code=result.exit_code,
                avg_fps=result.avg_fps,
                avg_speed=result.avg_speed,
                tmp_path=tmp_path,
                dry_run=dry_run,
                config=config,
                container=library_snapshot["container"],
            )
        except Exception as exc:
            logger.exception("Job %d : erreur inattendue", job_id)
            await self._finalize_error(work, f"erreur interne : {exc}")
            if tmp_path and os.path.exists(tmp_path):
                with contextlib.suppress(OSError):
                    os.unlink(tmp_path)
        finally:
            self._cancel_events.pop(job_id, None)
            info = self._active_jobs.pop(job_id, None)
            if info is not None:
                event_bus.publish_queue({"type": "job_finished", "job_id": job_id})

    async def _prepare(
        self,
        session: AsyncSession,
        work: _ClaimedWork,
        gpu_cap: GpuCapability,
        slot: int,
        config: RuntimeConfig,
    ) -> (
        tuple[
            MediaProbe,
            ResolvedRule,
            Any,
            dict[str, Any],
            list[str],
            str,
            bool,
            str,
        ]
        | None
    ):
        """Sonde fraîche, plan, règle, commande. None = job terminé en amont."""
        mf = await session.get(MediaFile, work.media_id)
        job = await session.get(Job, work.job_id)
        if mf is None or job is None:
            return None
        library = await session.get(Library, work.library_id)
        if library is None:
            await self._finalize_error(work, "bibliothèque supprimée")
            return None

        try:
            probe = await probe_file(mf.path, config.ffprobe_path)
        except ProbeError as exc:
            await self._finalize_error(work, f"sonde impossible : {exc}")
            return None

        plan = plan_treatment(
            probe,
            hdr_policy=library.hdr_policy,
            dv_policy=library.dv_policy,
            caps=self.capabilities.dv_caps,
        )
        if plan.action is TreatmentAction.SKIP:
            job.state = JobState.CANCELLED
            job.error_message = plan.reason
            job.finished_at = _now()
            mf.status = MediaStatus.SKIPPED_POLICY
            mf.last_error = plan.reason
            return None

        rules = (
            (
                await session.execute(
                    select(BitrateRule).where(BitrateRule.library_id == library.id)
                )
            )
            .scalars()
            .all()
        )
        if probe.resolution_bucket is None:
            await self._finalize_error(work, "résolution vidéo indéterminée")
            return None
        try:
            rule = resolve_rule(
                rules,
                codec=library.output_codec,
                bucket=probe.resolution_bucket,
                is_hdr=plan.effective_is_hdr,
                container=library.container,
                hdr_policy=library.hdr_policy,
                dv_policy=library.dv_policy,
            )
        except NoRuleError as exc:
            await self._finalize_error(work, str(exc))
            return None

        dry_run = library.dry_run if library.dry_run is not None else config.dry_run_global

        # Espace disque (§5.6) — estimation prudente : taille source
        src_size = mf.size_bytes or 0
        try:
            if dry_run:
                os.makedirs(config.dry_run_out_dir, exist_ok=True)
                check_free_space(
                    config.dry_run_out_dir,
                    needed_bytes=src_size,
                    min_free_gb=config.min_free_disk_gb,
                )
                tmp_path = make_dry_run_path(
                    mf.path,
                    library_path=library.path,
                    out_dir=config.dry_run_out_dir,
                    container=library.container,
                )
            else:
                check_free_space(
                    os.path.dirname(mf.path),
                    needed_bytes=src_size,
                    min_free_gb=config.min_free_disk_gb,
                )
                tmp_path = make_tmp_path(
                    mf.path,
                    container=library.container,
                    tmp_strategy=config.tmp_strategy,
                    tmp_dir=config.tmp_dir,
                )
        except DiskSpaceError as exc:
            await self._finalize_error(work, str(exc), retryable=True, config=config)
            return None

        try:
            cmd = build_ffmpeg_cmd(
                BuildParams(
                    ffmpeg_path=config.ffmpeg_path,
                    src_path=mf.path,
                    dst_path=tmp_path,
                    gpu_api=gpu_cap.effective_api,
                    drm_node=gpu_cap.drm_node,
                    rule=rule,
                    container=library.container,
                    plan=plan,
                    src_bit_depth=probe.bit_depth,
                    p5_zero_copy=gpu_cap.p5_zero_copy,
                )
            )
        except BuildError as exc:
            await self._finalize_error(work, str(exc))
            return None

        log_path = os.path.join(config.log_dir, f"job_{work.job_id}.log")

        # Snapshot « avant » + passage en running
        job.state = JobState.RUNNING
        job.dry_run = dry_run
        job.rule_hash = rule.rule_hash
        job.gpu_id = gpu_cap.gpu_id
        job.worker_slot = slot
        job.started_at = _now()
        job.src_path = mf.path
        job.src_size_bytes = mf.size_bytes
        job.src_codec = probe.video_codec
        job.src_bitrate_kbps = probe.video_bitrate_kbps
        job.src_resolution = f"{probe.width}x{probe.height}" if probe.width else None
        job.src_hdr_format = probe.hdr_format
        job.src_duration_s = (
            decimal.Decimal(f"{probe.duration_s:.3f}") if probe.duration_s else None
        )
        job.src_mtime = mf.mtime
        job.dst_path = tmp_path
        job.ffmpeg_cmd = cmd
        job.log_path = log_path

        library_snapshot: dict[str, Any] = {
            "container": library.container,
            "min_gain_percent": float(library.min_gain_percent),
            "path": library.path,
        }
        return probe, rule, plan, library_snapshot, cmd, tmp_path, dry_run, log_path

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

    async def _finalize_error(
        self,
        work: _ClaimedWork,
        message: str,
        *,
        retryable: bool = False,
        config: RuntimeConfig | None = None,
    ) -> None:
        async with session_scope() as session:
            job = await session.get(Job, work.job_id)
            mf = await session.get(MediaFile, work.media_id)
            if job is not None:
                job.state = JobState.FAILED
                job.error_message = message
                job.finished_at = _now()
            if mf is not None:
                mf.last_error = message
                if retryable and config is not None:
                    mf.retry_count += 1
                    if mf.retry_count > config.max_retries:
                        mf.status = MediaStatus.FAILED
                    else:
                        mf.status = MediaStatus.QUEUED
                        mf.next_attempt_at = _now() + datetime.timedelta(
                            seconds=retry_backoff_s(mf.retry_count)
                        )
                else:
                    mf.status = MediaStatus.FAILED
        event_bus.publish_queue(
            {"type": "job_failed", "job_id": work.job_id, "error": message}
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
        event_bus.publish_queue({"type": "job_cancelled", "job_id": work.job_id})

    async def _finalize(
        self,
        *,
        work: _ClaimedWork,
        probe: MediaProbe,
        rule: ResolvedRule,
        report: Any,
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

            if outcome is ValidationOutcome.NO_GAIN:
                # §5.5.7 : original conservé, fichier marqué traité
                with contextlib.suppress(OSError):
                    os.unlink(tmp_path)
                job.state = JobState.SKIPPED_NO_GAIN
                job.dst_path = None
                mf.status = MediaStatus.SKIPPED_NO_GAIN
                mf.applied_rule_hash = rule.rule_hash
                mf.last_error = None
                event_bus.publish_queue(
                    {"type": "job_no_gain", "job_id": work.job_id, "gain": report.gain_percent}
                )
                return

            if outcome is ValidationOutcome.FAILED:
                if not dry_run or os.path.dirname(tmp_path) != config.dry_run_out_dir:
                    with contextlib.suppress(OSError):
                        os.unlink(tmp_path)
                reason = report.failure_reason or "validation échouée"
                job.state = JobState.FAILED
                job.error_message = reason
                job.dst_path = None
                mf.last_error = reason
                mf.retry_count += 1
                if mf.retry_count > config.max_retries:
                    mf.status = MediaStatus.FAILED
                else:
                    mf.status = MediaStatus.QUEUED
                    mf.next_attempt_at = _now() + datetime.timedelta(
                        seconds=retry_backoff_s(mf.retry_count)
                    )
                event_bus.publish_queue(
                    {"type": "job_failed", "job_id": work.job_id, "error": reason}
                )
                return

            # --- PASSED -------------------------------------------------------
            if job.src_size_bytes and dst_size:
                job.bytes_saved = job.src_size_bytes - dst_size
                job.gain_percent = decimal.Decimal(f"{report.gain_percent:.2f}")

            if dry_run:
                # §9.4 : sortie conservée dans dry_run_out_dir, AUCUN remplacement
                job.state = JobState.SUCCESS
                job.dst_path = tmp_path
                mf.status = MediaStatus.DONE
                mf.applied_rule_hash = rule.rule_hash
                mf.processed_tool_version = TOOL_ID
                mf.last_error = None
                mf.retry_count = 0
                event_bus.publish_queue(
                    {
                        "type": "job_success",
                        "job_id": work.job_id,
                        "dry_run": True,
                        "gain": report.gain_percent,
                    }
                )
                return

            # Remplacement atomique (§5.6) — après les 9 validations UNIQUEMENT
            await self._set_job_state_obj(job, JobState.REPLACING)
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
                job.state = JobState.FAILED
                job.error_message = f"remplacement impossible : {exc}"
                mf.status = MediaStatus.FAILED
                mf.last_error = job.error_message
                event_bus.publish_queue(
                    {"type": "job_failed", "job_id": work.job_id, "error": str(exc)}
                )
                return

            job.state = JobState.SUCCESS
            job.dst_path = replace_result.final_path
            job.trashed_path = replace_result.trashed_path

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
            mf.retry_count = 0
            mf.next_attempt_at = None
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
            event_bus.publish_queue(
                {
                    "type": "job_success",
                    "job_id": work.job_id,
                    "dry_run": False,
                    "gain": report.gain_percent,
                }
            )

    @staticmethod
    async def _set_job_state_obj(job: Job, state: JobState) -> None:
        job.state = state


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
