"""Remplacement atomique de l'original (§5.6) + injection MDCV/CLL conteneur.

Garanties :
- l'encodage se fait dans `<dir_du_fichier>/.transcoder_tmp/` (même
  filesystem → `os.rename()` atomique). `data/tmp` n'est utilisé QUE si
  `st_dev` correspond (§9.3), sinon bascule silencieuse sur le sibling ;
- JAMAIS de `rm` sur un original : déplacement en corbeille (rétention
  configurable), sauf option `immediate_delete` explicitement activée ;
- fsync du fichier et des répertoires avant/après rename ;
- restauration mtime/atime, uid/gid et permissions de l'original (§5.6) ;
- rollback : `restore_from_trash()` remet un original en place.

Injection HDR (arbitrage 1) : quand la source ne portait pas de MDCV/CLL
(DV P5/P7), les métadonnées reconstruites sont posées au niveau conteneur
Matroska via mkvpropedit, AVANT la validation (qui vérifie leur présence).
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import time
import uuid
from dataclasses import dataclass

from transcoder.core.dovi import Hdr10Metadata
from transcoder.enums import Container

logger = logging.getLogger(__name__)

TMP_DIR_NAME = ".transcoder_tmp"


class ReplaceError(RuntimeError):
    """Échec du remplacement — l'original est TOUJOURS intact quand levée."""


class DiskSpaceError(ReplaceError):
    """Espace disque libre insuffisant pour encoder en sécurité."""


# ---------------------------------------------------------------------------
# Espace disque et chemins temporaires
# ---------------------------------------------------------------------------
def check_free_space(directory: str, *, needed_bytes: int, min_free_gb: float) -> None:
    """Refuse de lancer un encodage si l'espace libre est insuffisant (§5.6)."""
    usage = shutil.disk_usage(directory)
    reserve = int(min_free_gb * 1024**3)
    if usage.free < needed_bytes + reserve:
        raise DiskSpaceError(
            f"espace libre insuffisant sur {directory} : "
            f"{usage.free / 1024**3:.1f} Go libres, besoin estimé "
            f"{needed_bytes / 1024**3:.1f} Go + réserve {min_free_gb:.0f} Go"
        )


def make_tmp_path(
    src_path: str,
    *,
    container: Container,
    tmp_strategy: str,
    tmp_dir: str,
) -> str:
    """Chemin temporaire d'encodage. Stratégie `data_tmp` uniquement si même
    filesystem que la source (détection st_dev, §9.3), sinon sibling."""
    src_dir = os.path.dirname(src_path)
    base_dir = os.path.join(src_dir, TMP_DIR_NAME)
    if tmp_strategy == "data_tmp":
        try:
            if os.stat(tmp_dir).st_dev == os.stat(src_dir).st_dev:
                base_dir = tmp_dir
            else:
                logger.info(
                    "tmp_dir %s sur un autre filesystem que %s : bascule sur %s (§9.3)",
                    tmp_dir,
                    src_dir,
                    base_dir,
                )
        except OSError:
            logger.warning("tmp_dir %s inaccessible : bascule sur %s", tmp_dir, base_dir)
    os.makedirs(base_dir, exist_ok=True)
    return os.path.join(base_dir, f"{uuid.uuid4().hex}.{container.value}")


def make_dry_run_path(src_path: str, *, library_path: str, out_dir: str, container: Container) -> str:
    """Sortie dry-run : arborescence miroir sous dry_run_out_dir (§9.4)."""
    rel = os.path.relpath(src_path, library_path)
    stem, _ = os.path.splitext(rel)
    dst = os.path.join(out_dir, f"{stem}.{container.value}")
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    return dst


# ---------------------------------------------------------------------------
# fsync helpers
# ---------------------------------------------------------------------------
def _fsync_file(path: str) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_dir(path: str) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return  # certains FS refusent l'open de répertoire : non bloquant
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


# ---------------------------------------------------------------------------
# Remplacement atomique
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ReplaceResult:
    final_path: str
    trashed_path: str | None
    extension_changed: bool


def _move_to_trash(src_path: str, trash_dir: str, job_id: int) -> str:
    """Déplace l'original en corbeille. rename même FS, sinon copie+unlink
    (la copie est vérifiée par taille avant suppression de l'original)."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest_dir = os.path.join(trash_dir, f"{stamp}-job{job_id}")
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, os.path.basename(src_path))
    try:
        os.rename(src_path, dest)
    except OSError:
        # Corbeille sur un autre filesystem : copie contrôlée puis unlink
        src_size = os.path.getsize(src_path)
        shutil.copy2(src_path, dest)
        if os.path.getsize(dest) != src_size:
            os.unlink(dest)
            raise ReplaceError(
                f"copie vers la corbeille incomplète pour {src_path} — original conservé"
            ) from None
        os.unlink(src_path)
    return dest


def replace_original(
    *,
    src_path: str,
    tmp_path: str,
    job_id: int,
    trash_dir: str,
    immediate_delete: bool,
    container: Container,
) -> ReplaceResult:
    """Remplacement atomique (§5.6). À n'appeler qu'après les 9 validations.

    Restaure mtime/atime, uid/gid et permissions de l'original sur le
    nouveau fichier. Si l'extension change, l'ancien chemin part en corbeille
    et le nouveau chemin doit être mis à jour en base par l'appelant.
    """
    if not os.path.exists(tmp_path):
        raise ReplaceError(f"fichier temporaire absent : {tmp_path}")
    src_stat = os.stat(src_path)

    stem, src_ext = os.path.splitext(src_path)
    final_path = f"{stem}.{container.value}"
    extension_changed = src_ext.lstrip(".").lower() != container.value

    # 1) fsync du nouveau fichier avant toute destruction
    _fsync_file(tmp_path)

    # 2) l'original part en corbeille (jamais de rm direct, §5.6)
    trashed: str | None = None
    if immediate_delete:
        logger.warning("Suppression immédiate activée : %s ne passera pas par la corbeille", src_path)
    else:
        trashed = _move_to_trash(src_path, trash_dir, job_id)

    # 3) rename atomique du temporaire vers le chemin final. En mode
    # immediate_delete sans changement d'extension, ce rename écrase
    # atomiquement l'original (le chemin final EST src_path).
    try:
        os.rename(tmp_path, final_path)
    except OSError as exc:
        # Échec du rename : on remet l'original en place si possible
        if trashed is not None and not os.path.exists(src_path):
            os.rename(trashed, src_path)
        raise ReplaceError(f"rename atomique impossible : {exc}") from exc

    # 4) suppression immédiate demandée explicitement (désactivée par défaut) :
    # si l'extension a changé, l'original occupe encore src_path
    if immediate_delete and extension_changed and os.path.exists(src_path):
        os.unlink(src_path)

    # 5) restauration métadonnées de l'original (§5.6)
    try:
        os.utime(final_path, (src_stat.st_atime, src_stat.st_mtime))
    except OSError as exc:
        logger.warning("Restauration mtime impossible sur %s : %s", final_path, exc)
    try:
        os.chown(final_path, src_stat.st_uid, src_stat.st_gid)
    except (OSError, PermissionError) as exc:
        logger.warning("Restauration uid/gid impossible sur %s : %s", final_path, exc)
    try:
        os.chmod(final_path, src_stat.st_mode & 0o7777)
    except OSError as exc:
        logger.warning("Restauration permissions impossible sur %s : %s", final_path, exc)

    _fsync_dir(os.path.dirname(final_path))
    if trashed is not None:
        _fsync_dir(os.path.dirname(trashed))
    return ReplaceResult(
        final_path=final_path, trashed_path=trashed, extension_changed=extension_changed
    )


def restore_from_trash(trashed_path: str, original_path: str) -> None:
    """Rollback : remet un original depuis la corbeille (§9.5.8).

    Refuse d'écraser un fichier existant : la version transcodée doit être
    déplacée/supprimée explicitement d'abord.
    """
    if not os.path.exists(trashed_path):
        raise ReplaceError(f"fichier introuvable en corbeille : {trashed_path}")
    if os.path.exists(original_path):
        raise ReplaceError(
            f"{original_path} existe déjà — supprimer/déplacer la version transcodée d'abord"
        )
    os.makedirs(os.path.dirname(original_path), exist_ok=True)
    try:
        os.rename(trashed_path, original_path)
    except OSError:
        shutil.copy2(trashed_path, original_path)
        os.unlink(trashed_path)
    _fsync_dir(os.path.dirname(original_path))


# ---------------------------------------------------------------------------
# Nettoyage : corbeille expirée et temporaires orphelins
# ---------------------------------------------------------------------------
def purge_trash(trash_dir: str, retention_days: int) -> int:
    """Supprime les entrées de corbeille plus vieilles que la rétention."""
    if not os.path.isdir(trash_dir):
        return 0
    cutoff = time.time() - retention_days * 86400
    removed = 0
    for entry in os.scandir(trash_dir):
        try:
            if entry.stat().st_mtime < cutoff:
                if entry.is_dir():
                    shutil.rmtree(entry.path, ignore_errors=True)
                else:
                    os.unlink(entry.path)
                removed += 1
        except OSError:
            continue
    if removed:
        logger.info("Corbeille : %d entrée(s) expirée(s) purgée(s)", removed)
    return removed


def purge_old_logs(log_dir: str, retention_days: int) -> int:
    """Supprime les logs FFmpeg plus vieux que la rétention configurée."""
    if not os.path.isdir(log_dir):
        return 0
    cutoff = time.time() - retention_days * 86400
    removed = 0
    for entry in os.scandir(log_dir):
        try:
            if entry.is_file() and entry.stat().st_mtime < cutoff:
                os.unlink(entry.path)
                removed += 1
        except OSError:
            continue
    if removed:
        logger.info("Logs : %d fichier(s) expiré(s) purgé(s)", removed)
    return removed


def purge_orphan_tmp(library_paths: list[str], extra_tmp_dir: str | None = None) -> int:
    """Purge les fichiers d'encodage orphelins après un crash (§5.2)."""
    removed = 0
    roots: list[str] = []
    for lib in library_paths:
        for dirpath, dirnames, _files in os.walk(lib):
            if TMP_DIR_NAME in dirnames:
                roots.append(os.path.join(dirpath, TMP_DIR_NAME))
    if extra_tmp_dir and os.path.isdir(extra_tmp_dir):
        roots.append(extra_tmp_dir)
    for root in roots:
        try:
            for entry in os.scandir(root):
                try:
                    if entry.is_file():
                        os.unlink(entry.path)
                        removed += 1
                except OSError:
                    continue
        except OSError:
            continue
    if removed:
        logger.info("Temporaires orphelins purgés : %d fichier(s)", removed)
    return removed


# ---------------------------------------------------------------------------
# Injection MDCV/CLL au niveau conteneur Matroska (arbitrage 1)
# ---------------------------------------------------------------------------
def mkvpropedit_args(mkv_path: str, meta: Hdr10Metadata) -> list[str]:
    """Arguments mkvpropedit pour poser MasteringMetadata + MaxCLL/MaxFALL
    sur la piste vidéo (fonction pure, testée sans binaire)."""
    m = meta.mastering
    return [
        mkv_path,
        "--edit",
        "track:v1",
        "--set", f"chromaticity-coordinates-red-x={m.red_x:.5f}",
        "--set", f"chromaticity-coordinates-red-y={m.red_y:.5f}",
        "--set", f"chromaticity-coordinates-green-x={m.green_x:.5f}",
        "--set", f"chromaticity-coordinates-green-y={m.green_y:.5f}",
        "--set", f"chromaticity-coordinates-blue-x={m.blue_x:.5f}",
        "--set", f"chromaticity-coordinates-blue-y={m.blue_y:.5f}",
        "--set", f"white-coordinates-x={m.white_x:.5f}",
        "--set", f"white-coordinates-y={m.white_y:.5f}",
        "--set", f"min-luminance={m.min_luminance:.4f}",
        "--set", f"max-luminance={m.max_luminance:.4f}",
        "--set", f"max-content-light={meta.content_light.max_cll}",
        "--set", f"max-frame-light={meta.content_light.max_fall}",
    ]


async def inject_hdr10_metadata_mkv(
    mkv_path: str, meta: Hdr10Metadata, *, mkvpropedit_path: str
) -> bool:
    """Pose MDCV/CLL au niveau conteneur (ffprobe les relit en side data)."""
    proc = await asyncio.create_subprocess_exec(
        mkvpropedit_path,
        *mkvpropedit_args(mkv_path, meta),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    stdout, _ = await proc.communicate()
    if proc.returncode != 0:
        logger.error(
            "mkvpropedit a échoué sur %s : %s",
            mkv_path,
            stdout.decode("utf-8", "replace")[-300:],
        )
        return False
    logger.info(
        "MDCV/CLL injectés au conteneur de %s (source des valeurs : %s)",
        mkv_path,
        meta.source,
    )
    return True
