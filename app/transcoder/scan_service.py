"""Orchestration des scans : déclenchement manuel (API) + cron APScheduler.

Chaque bibliothèque a son propre cron (`libraries.scan_cron`) ; le
scheduler est rechargé quand la config des bibliothèques change.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
from collections.abc import Callable
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select

from transcoder.core.dovi import DvCapabilities
from transcoder.core.jellyfin import fetch_dates_created
from transcoder.core.replace import purge_old_logs, purge_trash
from transcoder.core.scanner import ScanResult, scan_library
from transcoder.db import session_scope
from transcoder.events import event_bus
from transcoder.models import Library
from transcoder.runtime_config import load_runtime_config

logger = logging.getLogger(__name__)

# Un seul scan à la fois : un scan cron et un scan manuel simultanés
# n'apportent que des conflits d'upsert.
_scan_lock = asyncio.Lock()


async def run_scan(
    dv_caps: DvCapabilities, library_ids: list[int] | None = None
) -> dict[int, dict[str, Any]]:
    """Scanne les bibliothèques activées (toutes, ou celles demandées)."""
    async with _scan_lock:
        results: dict[int, dict[str, Any]] = {}
        async with session_scope() as session:
            config = await load_runtime_config(session)
            stmt = select(Library).where(Library.enabled.is_(True))
            if library_ids:
                stmt = select(Library).where(Library.id.in_(library_ids))
            library_ids_to_scan = [
                lib.id for lib in (await session.execute(stmt)).scalars().all()
            ]

        jellyfin_dates: dict[str, datetime.datetime] = {}
        if config.jellyfin_url and config.jellyfin_api_key:
            jellyfin_dates = await fetch_dates_created(
                config.jellyfin_url, config.jellyfin_api_key
            )

        # Une session/transaction PAR bibliothèque : sur 70k fichiers, une
        # transaction unique rendrait le scan invisible pendant des heures.
        for lib_id in library_ids_to_scan:
            async with session_scope() as session:
                library = await session.get(Library, lib_id)
                if library is None:
                    continue
                event_bus.publish_queue(
                    {"type": "scan_started", "library_id": library.id, "name": library.name}
                )

                async def on_progress(
                    done: int, total: int, _lib_id: int = library.id
                ) -> None:
                    event_bus.publish_queue(
                        {
                            "type": "scan_progress",
                            "library_id": _lib_id,
                            "done": done,
                            "total": total,
                        }
                    )

                result: ScanResult = await scan_library(
                    session,
                    library,
                    config=config,
                    dv_caps=dv_caps,
                    jellyfin_dates=jellyfin_dates,
                    on_progress=on_progress,
                )
                results[library.id] = result.as_dict()
            event_bus.publish_queue(
                {
                    "type": "scan_finished",
                    "library_id": lib_id,
                    "result": results.get(lib_id, {}),
                }
            )
        return results


async def run_maintenance() -> dict[str, int]:
    """Maintenance quotidienne : rétention corbeille (§5.6) et logs."""
    async with session_scope() as session:
        config = await load_runtime_config(session)
    trashed = await asyncio.to_thread(
        purge_trash, config.trash_dir, config.trash_retention_days
    )
    logs = await asyncio.to_thread(
        purge_old_logs, config.log_dir, config.log_retention_days
    )
    return {"trash_purged": trashed, "logs_purged": logs}


class ScanScheduler:
    """Crons de scan par bibliothèque (APScheduler), rechargés à chaud."""

    def __init__(self, dv_caps_getter: Callable[[], DvCapabilities]) -> None:
        self._scheduler = AsyncIOScheduler()
        self._dv_caps_getter = dv_caps_getter

    def start(self) -> None:
        self._scheduler.start()

    def shutdown(self) -> None:
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)

    async def reload(self) -> None:
        """Reconstruit tous les jobs cron depuis la table libraries."""
        self._scheduler.remove_all_jobs()
        async with session_scope() as session:
            libraries = (
                (await session.execute(select(Library).where(Library.enabled.is_(True))))
                .scalars()
                .all()
            )
            for lib in libraries:
                if not lib.scan_cron:
                    continue
                try:
                    trigger = CronTrigger.from_crontab(lib.scan_cron)
                except ValueError as exc:
                    logger.error(
                        "Cron invalide pour %s (« %s ») : %s", lib.name, lib.scan_cron, exc
                    )
                    continue
                self._scheduler.add_job(
                    run_scan,
                    trigger,
                    args=[self._dv_caps_getter(), [lib.id]],
                    id=f"scan-lib-{lib.id}",
                    replace_existing=True,
                    coalesce=True,
                    max_instances=1,
                )
        # Maintenance quotidienne : rétention corbeille + logs (04:30)
        self._scheduler.add_job(
            run_maintenance,
            CronTrigger.from_crontab("30 4 * * *"),
            id="maintenance",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
        )
        logger.info("Scheduler de scans rechargé : %d cron(s)", len(self._scheduler.get_jobs()))
