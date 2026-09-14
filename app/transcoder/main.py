"""Application FastAPI : lifespan complet + montage API/WS/frontend.

Séquence de démarrage (plan module 5) :
1. migrations Alembic (thread, l'env alembic fait son propre asyncio.run) ;
2. seed idempotent (bibliothèques/règles/GPU si vide, settings if-missing) ;
3. détection des capacités (encodeurs, libplacebo DV, chaîne P5) ;
4. reprise après crash (jobs → queued, purge des tmp orphelins) ;
5. démarrage des workers + scheduler de scans.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from pathlib import Path
from sqlalchemy import select
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import Response
from starlette.types import Scope

from transcoder.api.routers import files, gpus, jobs, libraries, queue, stats, system, ws
from transcoder.config import get_settings
from transcoder.core.capabilities import GpuConfig, detect_capabilities
from transcoder.core.queue import (
    QueueManager,
    backfill_failure_categories,
    cleanup_orphan_tmp,
    recover_after_crash,
)
from transcoder.db import dispose_engine, session_scope
from transcoder.migrate import run_migrations
from transcoder.models import Gpu
from transcoder.runtime_config import load_runtime_config
from transcoder.scan_service import ScanScheduler
from transcoder.seed import apply_seed

logger = logging.getLogger(__name__)

def _resolve_web_dist() -> Path:
    """Build frontend : WEB_DIST_DIR (docker) sinon ../web/dist du repo."""
    env = os.environ.get("WEB_DIST_DIR")
    if env:
        return Path(env)
    here = Path(__file__).resolve()
    for parent_idx in (2, 1):
        candidate = here.parents[parent_idx] / "web" / "dist"
        if candidate.is_dir():
            return candidate
    return here.parents[2] / "web" / "dist"


_WEB_DIST = _resolve_web_dist()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s : %(message)s",
    )
    settings = get_settings()

    # 1. Migrations — env.py alembic est async : exécution hors boucle
    await asyncio.to_thread(run_migrations)

    # 2. Seed idempotent
    async with session_scope() as session:
        await apply_seed(session, settings)

    # 3. Capacités (encodeurs, libplacebo DV, zéro-copie P5)
    async with session_scope() as session:
        config = await load_runtime_config(session)
        gpu_rows = (await session.execute(select(Gpu))).scalars().all()
        gpu_configs = [
            GpuConfig(id=g.id, name=g.name, drm_node=g.drm_node, api=g.api)
            for g in gpu_rows
            if g.enabled
        ]
    capabilities = await detect_capabilities(
        ffmpeg_path=config.ffmpeg_path,
        ffprobe_path=config.ffprobe_path,
        dovi_tool_path=config.dovi_tool_path,
        mkvpropedit_path=config.mkvpropedit_path,
        gpus=gpu_configs,
    )
    app.state.capabilities = capabilities

    # 4. Reprise après crash (§5.2)
    async with session_scope() as session:
        await recover_after_crash(session)
    async with session_scope() as session:
        await cleanup_orphan_tmp(session, config)

    # 5. Workers + scheduler
    queue_manager = QueueManager(capabilities)
    app.state.queue_manager = queue_manager
    await queue_manager.start()

    scheduler = ScanScheduler(lambda: app.state.capabilities.dv_caps)
    app.state.scan_scheduler = scheduler
    scheduler.start()
    await scheduler.reload()

    # 6. Reclassement de l'historique des échecs (tâche de fond, non bloquante)
    backfill_task = asyncio.create_task(_run_backfill(), name="backfill-failures")

    logger.info("Transcodeur démarré (dry_run_global=%s)", config.dry_run_global)
    try:
        yield
    finally:
        backfill_task.cancel()
        scheduler.shutdown()
        await queue_manager.stop()
        await dispose_engine()


async def _run_backfill() -> None:
    try:
        await backfill_failure_categories()
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Reclassement de l'historique des échecs interrompu")


class SpaStaticFiles(StaticFiles):
    """Fichiers statiques avec fallback SPA : toute route inconnue (hors
    /api et /ws, matchées avant le mount) sert index.html pour que le
    routeur React gère les liens profonds et le rafraîchissement.

    Cache : index.html est toujours revalidé (sinon le navigateur garde
    l'ancienne interface après une mise à jour, faute d'en-tête de cache),
    les assets Vite (noms hachés par contenu) sont immuables. Un asset
    absent reste un 404 : lui renvoyer index.html casserait le chargement."""

    _ASSETS_PREFIX = "assets/"

    async def get_response(self, path: str, scope: Scope) -> Response:
        is_asset = path.startswith(self._ASSETS_PREFIX)
        try:
            response = await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code == 404 and not is_asset:
                return await self._index(scope)
            raise
        if response.status_code == 404 and not is_asset:
            return await self._index(scope)
        response.headers["Cache-Control"] = (
            "public, max-age=31536000, immutable" if is_asset else "no-cache"
        )
        return response

    async def _index(self, scope: Scope) -> Response:
        response = await super().get_response("index.html", scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


def create_app() -> FastAPI:
    app = FastAPI(
        title="Transcodeur Jellyfin",
        version="1.0.0",
        lifespan=lifespan,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )
    api_prefix = "/api/v1"
    app.include_router(libraries.router, prefix=api_prefix)
    app.include_router(gpus.router, prefix=api_prefix)
    app.include_router(queue.router, prefix=api_prefix)
    app.include_router(jobs.router, prefix=api_prefix)
    app.include_router(files.router, prefix=api_prefix)
    app.include_router(stats.router, prefix=api_prefix)
    app.include_router(system.router, prefix=api_prefix)
    app.include_router(ws.router)

    # Frontend build (module web) servi en statique ; les routes /api et /ws
    # sont déclarées avant le mount et gardent la priorité.
    if _WEB_DIST.is_dir():
        app.mount("/", SpaStaticFiles(directory=str(_WEB_DIST), html=True), name="web")
    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "transcoder.main:app",
        host="0.0.0.0",
        port=get_settings().http_port,
        log_level="info",
    )
