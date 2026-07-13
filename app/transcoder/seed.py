"""Seed initial de la base, exécuté au démarrage de l'application.

Choix d'implémentation (vs seed dans une migration Alembic) :
- idempotent : bibliothèques/règles/GPU créés uniquement si la table
  `libraries` est vide (l'utilisateur peut tout supprimer sans que ça
  revienne) ; app_settings en insert-if-missing clé par clé (une mise à
  jour de l'app peut introduire de nouvelles clés sans écraser l'existant) ;
- les valeurs initiales dépendent du .env (DRY_RUN, rétention…), ce qui
  n'a pas sa place figé dans l'historique de migrations.
"""

from __future__ import annotations

import logging

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from transcoder.config import Settings
from transcoder.enums import Codec, Container, DvPolicy, GpuApi, HdrPolicy
from transcoder.models import AppSetting, BitrateRule, Gpu, Library
from transcoder.seed_data import (
    SEED_GPU,
    SEED_LIBRARIES,
    default_app_settings,
    iter_seed_rules,
)

logger = logging.getLogger(__name__)


async def apply_seed(session: AsyncSession, settings: Settings) -> bool:
    """Applique le seed. Retourne True si les bibliothèques ont été créées."""
    created = await _seed_libraries_rules_gpu(session)
    await _seed_app_settings(session, settings)
    return created


async def _seed_libraries_rules_gpu(session: AsyncSession) -> bool:
    count = await session.scalar(select(func.count()).select_from(Library))
    if count is not None and count > 0:
        logger.info("Seed bibliothèques ignoré : %d bibliothèque(s) déjà présente(s)", count)
        return False

    libs: dict[str, Library] = {}
    for name, path in SEED_LIBRARIES:
        lib = Library(
            name=name,
            path=path,
            enabled=True,
            output_codec=Codec.HEVC,
            container=Container.MKV,
            hdr_policy=HdrPolicy.PRESERVE,
            dv_policy=DvPolicy.CONVERT_TO_HDR10,
            priority=0,
            scan_cron="0 3 * * *",
        )
        session.add(lib)
        libs[name] = lib
    await session.flush()  # attribue les IDs

    rules = iter_seed_rules()
    for r in rules:
        session.add(
            BitrateRule(
                library_id=libs[r.library_name].id,
                codec=r.codec,
                resolution_bucket=r.bucket,
                is_hdr=r.is_hdr,
                target_bitrate_kbps=r.target_kbps,
                max_bitrate_kbps=r.max_kbps,
                bufsize_kbps=r.bufsize_kbps,
            )
        )

    gpu_count = await session.scalar(select(func.count()).select_from(Gpu))
    if gpu_count == 0:
        name, drm_node, api, workers = SEED_GPU
        session.add(
            Gpu(
                name=name,
                drm_node=drm_node,
                api=GpuApi(api),
                worker_count=workers,
                enabled=True,
            )
        )

    logger.info(
        "Seed appliqué : %d bibliothèques, %d règles de bitrate, GPU initial",
        len(libs),
        len(rules),
    )
    return True


async def _seed_app_settings(session: AsyncSession, settings: Settings) -> None:
    """Insert-if-missing : ne touche jamais une clé existante."""
    defaults = default_app_settings(
        dry_run=settings.dry_run,
        trash_retention_days=settings.trash_retention_days,
        max_concurrent_jobs=settings.max_concurrent_jobs,
        data_dir=settings.data_dir,
    )
    for key, value in defaults.items():
        stmt = (
            pg_insert(AppSetting)
            .values(key=key, value=value)
            .on_conflict_do_nothing(index_elements=[AppSetting.key])
        )
        await session.execute(stmt)
