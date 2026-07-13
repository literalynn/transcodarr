"""Scanner incrémental des bibliothèques (§5.1) + marqueur anti-boucle (§5.7).

Déroulé par fichier :
1. filtres : extension, exclude_globs, date d'ajout (btime → fallback
   max(mtime, ctime), surcharge Jellyfin DateCreated si configurée) ;
2. sonde ffprobe — SAUF si (path, size, mtime) inchangé et déjà sondé
   (scan incrémental) : la sonde est alors reconstruite depuis la base ;
3. décision anti-boucle §5.7 (fonction pure `decide_status`) : re-traiter
   UNIQUEMENT si le tag conteneur est absent OU si le rule_hash applicable
   a changé (bascule de codec, bitrates modifiés, politiques…).
"""

from __future__ import annotations

import datetime
import decimal
import fnmatch
import logging
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from transcoder import TOOL_ID
from transcoder.core.buckets import bucket_for_height
from transcoder.core.dovi import DvCapabilities, TreatmentAction, TreatmentPlan, plan_treatment
from transcoder.core.probe import MediaProbe, ProbeError, probe_file
from transcoder.core.replace import TMP_DIR_NAME
from transcoder.core.rules import NoRuleError, parse_transcoder_tag, resolve_rule
from transcoder.enums import HdrFormat, MediaStatus, ResolutionBucket
from transcoder.models import BitrateRule, Library, MediaFile
from transcoder.runtime_config import RuntimeConfig

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ScanResult:
    scanned: int = 0
    new: int = 0
    updated: int = 0
    unchanged: int = 0
    queued: int = 0
    requeued_rule_change: int = 0
    skipped_policy: int = 0
    ignored_date: int = 0
    failed_probe: int = 0
    removed: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "scanned": self.scanned,
            "new": self.new,
            "updated": self.updated,
            "unchanged": self.unchanged,
            "queued": self.queued,
            "requeued_rule_change": self.requeued_rule_change,
            "skipped_policy": self.skipped_policy,
            "ignored_date": self.ignored_date,
            "failed_probe": self.failed_probe,
            "removed": self.removed,
            "errors": self.errors[:50],
        }


# ---------------------------------------------------------------------------
# Décision anti-boucle (§5.7) — fonction pure
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class FileFacts:
    """Faits nécessaires à la décision de mise en queue d'un fichier."""

    plan_action: TreatmentAction
    rule_hash: str | None  # None = aucune règle applicable
    container_tag: str | None  # tag TRANSCODER_TAG lu dans le fichier
    date_ok: bool
    existing_status: MediaStatus | None
    existing_applied_hash: str | None
    existing_retry_count: int
    max_retries: int


@dataclass(frozen=True, slots=True)
class Decision:
    status: MediaStatus
    reason: str
    rule_changed: bool = False


def decide_status(facts: FileFacts) -> Decision:
    """Décision §5.7 : un fichier est re-traité UNIQUEMENT si le tag est
    absent ou si le hash de la règle applicable a changé."""
    # Statut ignoré posé par l'utilisateur : jamais touché par le scan
    if facts.existing_status is MediaStatus.IGNORED:
        return Decision(MediaStatus.IGNORED, "ignoré par l'utilisateur")

    # En cours de traitement : le scan ne s'en mêle pas
    if facts.existing_status in (MediaStatus.QUEUED, MediaStatus.PROCESSING):
        return Decision(facts.existing_status, "déjà en file/en cours")

    if not facts.date_ok:
        return Decision(MediaStatus.IGNORED, "antérieur au filtre de date de la bibliothèque")

    if facts.plan_action is TreatmentAction.SKIP:
        return Decision(MediaStatus.SKIPPED_POLICY, "politique/capacités : non traité")

    if facts.rule_hash is None:
        return Decision(MediaStatus.FAILED, "aucune règle de bitrate applicable")

    # Tag conteneur présent : fichier produit par cette app
    if facts.container_tag is not None:
        parsed = parse_transcoder_tag(facts.container_tag)
        if parsed is not None:
            tool_id, tag_hash = parsed
            if tool_id == TOOL_ID:
                if tag_hash == facts.rule_hash:
                    return Decision(MediaStatus.DONE, "déjà traité, config inchangée")
                return Decision(
                    MediaStatus.QUEUED,
                    "règle modifiée depuis le dernier traitement : re-traitement",
                    rule_changed=True,
                )

    # Pas de tag : jamais traité par cette app (ou tag illisible)
    if facts.existing_status in (MediaStatus.SKIPPED_NO_GAIN, MediaStatus.FAILED):
        if facts.existing_applied_hash == facts.rule_hash:
            # skipped_no_gain : marqué traité, jamais retenté sans changement (§5.5.7)
            # failed avec retries épuisés : plus repris automatiquement (§5.2)
            if facts.existing_status is MediaStatus.SKIPPED_NO_GAIN:
                return Decision(MediaStatus.SKIPPED_NO_GAIN, "gain insuffisant (déjà évalué)")
            if facts.existing_retry_count > facts.max_retries:
                return Decision(MediaStatus.FAILED, "échecs répétés : ne sera plus retenté")
        else:
            return Decision(
                MediaStatus.QUEUED, "règle modifiée : nouvelle tentative", rule_changed=True
            )

    # DONE en base sans tag dans le fichier : le fichier a été remplacé/modifié
    # hors de l'app, ou dry-run précédent → re-queue légitime.
    return Decision(MediaStatus.QUEUED, "à traiter (tag absent)")


# ---------------------------------------------------------------------------
# Reconstruction d'une sonde depuis la base (scan incrémental)
# ---------------------------------------------------------------------------
def probe_from_row(mf: MediaFile) -> MediaProbe:
    """Sonde minimale reconstruite depuis media_files (fichier inchangé)."""
    return MediaProbe(
        path=mf.path,
        format_name=None,
        duration_s=float(mf.duration_s) if mf.duration_s is not None else None,
        video_codec=mf.video_codec,
        width=mf.width,
        height=mf.height,
        resolution_bucket=mf.resolution_bucket,
        video_bitrate_kbps=mf.video_bitrate_kbps,
        pix_fmt=mf.pix_fmt,
        bit_depth=mf.bit_depth,
        color_primaries=mf.color_primaries,
        color_transfer=mf.color_transfer,
        color_space=mf.color_space,
        color_range=None,
        is_hdr=bool(mf.is_hdr),
        hdr_format=mf.hdr_format or HdrFormat.SDR,
        dv_profile=mf.dv_profile,
        dv_bl_compat_id=mf.dv_bl_compat_id,
        dv_has_el=mf.dv_has_el,
        dv_has_rpu=None,
        mastering_display=None,
        content_light=None,
        audio_streams=list(mf.audio_streams or []),
        subtitle_streams=list(mf.subtitle_streams or []),
        transcoder_tag=mf.transcoder_tag,
    )


# ---------------------------------------------------------------------------
# Filtres de parcours
# ---------------------------------------------------------------------------
def file_matches_filters(
    rel_path: str, *, include_extensions: list[str], exclude_globs: list[str]
) -> bool:
    ext = os.path.splitext(rel_path)[1].lstrip(".").lower()
    if ext not in {e.lstrip(".").lower() for e in include_extensions}:
        return False
    basename = os.path.basename(rel_path)
    for pattern in exclude_globs:
        if fnmatch.fnmatch(rel_path, pattern) or fnmatch.fnmatch(basename, pattern):
            return False
    return True


def added_date(
    st: os.stat_result, jellyfin_date: datetime.datetime | None
) -> datetime.datetime:
    """Date d'ajout : Jellyfin DateCreated > btime (statx) > max(mtime, ctime)."""
    if jellyfin_date is not None:
        return jellyfin_date
    btime = getattr(st, "st_birthtime", None)
    if isinstance(btime, float) and btime > 0:
        return datetime.datetime.fromtimestamp(btime, tz=datetime.timezone.utc)
    return datetime.datetime.fromtimestamp(
        max(st.st_mtime, st.st_ctime), tz=datetime.timezone.utc
    )


def _walk_media_files(root: str) -> list[str]:
    out: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        # Ne jamais descendre dans les dossiers temporaires d'encodage
        dirnames[:] = [d for d in dirnames if d != TMP_DIR_NAME and not d.startswith(".")]
        for name in filenames:
            if not name.startswith("."):
                out.append(os.path.join(dirpath, name))
    return sorted(out)


def estimate_gain_bytes(
    src_bitrate_kbps: int | None, target_kbps: int, duration_s: float | None
) -> int | None:
    """Gain potentiel estimé : (bitrate source − cible) × durée (§5.2)."""
    if src_bitrate_kbps is None or duration_s is None:
        return None
    delta_kbps = src_bitrate_kbps - target_kbps
    return max(0, int(delta_kbps * duration_s * 125))  # 1 kbps = 125 o/s


def effective_video_bitrate_kbps(
    probe_bitrate_kbps: int | None, size_bytes: int, duration_s: float | None
) -> int | None:
    """Bitrate vidéo pour l'estimation de gain : celui de la sonde, sinon
    approximé depuis taille/durée (MKV sans bit_rate de flux ni tag BPS).
    Le facteur 0,85 retranche une part audio/conteneur forfaitaire."""
    if probe_bitrate_kbps is not None:
        return probe_bitrate_kbps
    if duration_s is None or duration_s <= 0 or size_bytes <= 0:
        return None
    return int(size_bytes * 8 / duration_s / 1000 * 0.85)


# ---------------------------------------------------------------------------
# Scan d'une bibliothèque
# ---------------------------------------------------------------------------
ProgressCallback = Callable[[int, int], Awaitable[None]]

_COMMIT_EVERY = 200


async def scan_library(
    session: AsyncSession,
    library: Library,
    *,
    config: RuntimeConfig,
    dv_caps: DvCapabilities,
    jellyfin_dates: dict[str, datetime.datetime] | None = None,
    on_progress: ProgressCallback | None = None,
) -> ScanResult:
    """Scan incrémental d'une bibliothèque.

    Sur une grosse bibliothèque (des dizaines de milliers de fichiers), la
    sonde prend des heures : commit PAR LOTS (_COMMIT_EVERY fichiers) pour
    que l'état soit visible dans l'UI au fil de l'eau, progression loggée
    et remontée via on_progress. Le commit final reste à l'appelant."""
    result = ScanResult()
    if not os.path.isdir(library.path):
        result.errors.append(f"chemin inexistant : {library.path}")
        logger.error("Scan %s : chemin inexistant %s", library.name, library.path)
        return result

    rules = (
        (
            await session.execute(
                select(BitrateRule).where(BitrateRule.library_id == library.id)
            )
        )
        .scalars()
        .all()
    )
    existing_rows = (
        (
            await session.execute(
                select(MediaFile).where(MediaFile.library_id == library.id)
            )
        )
        .scalars()
        .all()
    )
    by_path: dict[str, MediaFile] = {m.path: m for m in existing_rows}
    seen: set[str] = set()
    jf_dates = jellyfin_dates or {}

    all_paths = _walk_media_files(library.path)
    total = len(all_paths)
    logger.info("Scan %s : %d fichiers à examiner", library.name, total)
    processed = 0

    for path in all_paths:
        processed += 1
        if processed % _COMMIT_EVERY == 0:
            await session.commit()
            logger.info(
                "Scan %s : %d/%d fichiers examinés (%d en queue)",
                library.name,
                processed,
                total,
                result.queued,
            )
            if on_progress is not None:
                await on_progress(processed, total)
        rel = os.path.relpath(path, library.path)
        if not file_matches_filters(
            rel,
            include_extensions=library.include_extensions,
            exclude_globs=library.exclude_globs,
        ):
            continue
        result.scanned += 1
        seen.add(path)
        try:
            st = os.stat(path)
        except OSError as exc:
            result.errors.append(f"{path} : stat impossible ({exc})")
            continue

        mtime = datetime.datetime.fromtimestamp(st.st_mtime, tz=datetime.timezone.utc)
        ctime = datetime.datetime.fromtimestamp(st.st_ctime, tz=datetime.timezone.utc)
        added = added_date(st, jf_dates.get(path))
        date_ok = library.date_filter_after is None or added > library.date_filter_after

        mf = by_path.get(path)
        unchanged = (
            mf is not None
            and mf.size_bytes == st.st_size
            and mf.mtime is not None
            and abs(mf.mtime.timestamp() - st.st_mtime) < 1.0
            and mf.video_codec is not None
        )

        # --- Sonde (incrémentale §5.1) -------------------------------------
        probe: MediaProbe
        if unchanged and mf is not None:
            probe = probe_from_row(mf)
            result.unchanged += 1
        else:
            try:
                probe = await probe_file(path, config.ffprobe_path)
            except ProbeError as exc:
                result.failed_probe += 1
                if mf is None:
                    mf = MediaFile(library_id=library.id, path=path)
                    session.add(mf)
                    by_path[path] = mf
                mf.size_bytes = st.st_size
                mf.mtime = mtime
                mf.ctime = ctime
                mf.status = MediaStatus.FAILED
                mf.last_error = f"sonde ffprobe impossible : {exc}"
                continue
            if mf is None:
                mf = MediaFile(library_id=library.id, path=path)
                session.add(mf)
                by_path[path] = mf
                result.new += 1
            else:
                result.updated += 1
            _apply_probe(mf, probe, st, mtime, ctime, added)

        assert mf is not None

        # --- Plan de traitement + règle -------------------------------------
        plan = plan_treatment(
            probe,
            hdr_policy=library.hdr_policy,
            dv_policy=library.dv_policy,
            caps=dv_caps,
        )
        rule_hash: str | None = None
        target_kbps: int | None = None
        if probe.resolution_bucket is not None:
            try:
                resolved = resolve_rule(
                    rules,
                    codec=library.output_codec,
                    bucket=probe.resolution_bucket,
                    is_hdr=plan.effective_is_hdr,
                    container=library.container,
                    hdr_policy=library.hdr_policy,
                    dv_policy=library.dv_policy,
                )
                rule_hash = resolved.rule_hash
                target_kbps = resolved.target_kbps
            except NoRuleError:
                rule_hash = None

        decision = decide_status(
            FileFacts(
                plan_action=plan.action,
                rule_hash=rule_hash,
                container_tag=probe.transcoder_tag,
                date_ok=date_ok,
                existing_status=mf.status,
                existing_applied_hash=mf.applied_rule_hash,
                existing_retry_count=mf.retry_count,
                max_retries=config.max_retries,
            )
        )
        _apply_decision(mf, decision, plan, result)

        if target_kbps is not None:
            mf.estimated_gain_bytes = estimate_gain_bytes(
                effective_video_bitrate_kbps(
                    probe.video_bitrate_kbps, st.st_size, probe.duration_s
                ),
                target_kbps,
                probe.duration_s,
            )

    # --- Fichiers disparus ----------------------------------------------------
    for path, mf in by_path.items():
        if path not in seen and mf.status is not MediaStatus.PROCESSING:
            await session.delete(mf)
            result.removed += 1

    logger.info(
        "Scan %s : %d fichiers, %d nouveaux, %d en queue (%d re-queue config), "
        "%d skipped_policy, %d hors date, %d supprimés",
        library.name,
        result.scanned,
        result.new,
        result.queued,
        result.requeued_rule_change,
        result.skipped_policy,
        result.ignored_date,
        result.removed,
    )
    return result


def _apply_probe(
    mf: MediaFile,
    probe: MediaProbe,
    st: os.stat_result,
    mtime: datetime.datetime,
    ctime: datetime.datetime,
    added: datetime.datetime,
) -> None:
    mf.size_bytes = st.st_size
    mf.mtime = mtime
    mf.ctime = ctime
    mf.btime = added
    mf.duration_s = (
        None if probe.duration_s is None else decimal.Decimal(f"{probe.duration_s:.3f}")
    )
    mf.video_codec = probe.video_codec
    mf.width = probe.width
    mf.height = probe.height
    mf.resolution_bucket = (
        probe.resolution_bucket
        if probe.resolution_bucket is not None
        else (bucket_for_height(probe.height) if probe.height else None)
    )
    mf.video_bitrate_kbps = probe.video_bitrate_kbps
    mf.pix_fmt = probe.pix_fmt
    mf.bit_depth = probe.bit_depth
    mf.color_primaries = probe.color_primaries
    mf.color_transfer = probe.color_transfer
    mf.color_space = probe.color_space
    mf.is_hdr = probe.is_hdr
    mf.hdr_format = probe.hdr_format
    mf.dv_profile = probe.dv_profile
    mf.dv_bl_compat_id = probe.dv_bl_compat_id
    mf.dv_has_el = probe.dv_has_el
    mf.audio_streams = probe.audio_streams
    mf.subtitle_streams = probe.subtitle_streams
    mf.probe = probe.raw or None
    mf.transcoder_tag = probe.transcoder_tag


def _apply_decision(
    mf: MediaFile, decision: Decision, plan: TreatmentPlan, result: ScanResult
) -> None:
    previous = mf.status
    mf.status = decision.status
    if decision.status is MediaStatus.QUEUED:
        result.queued += 1
        if decision.rule_changed:
            result.requeued_rule_change += 1
            mf.retry_count = 0
            mf.next_attempt_at = None
        mf.last_error = None
    elif decision.status is MediaStatus.SKIPPED_POLICY:
        result.skipped_policy += 1
        mf.last_error = plan.reason
    elif decision.status is MediaStatus.IGNORED and previous is not MediaStatus.IGNORED:
        result.ignored_date += 1
        mf.last_error = decision.reason
    elif decision.status is MediaStatus.FAILED and previous is not MediaStatus.FAILED:
        mf.last_error = decision.reason


__all__ = [
    "Decision",
    "FileFacts",
    "ScanResult",
    "added_date",
    "decide_status",
    "estimate_gain_bytes",
    "file_matches_filters",
    "probe_from_row",
    "scan_library",
]
