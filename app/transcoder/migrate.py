"""Exécution programmatique des migrations Alembic au démarrage.

alembic/env.py fait son propre asyncio.run() : cette fonction est donc
synchrone et DOIT être appelée hors boucle d'événements. Depuis le
lifespan FastAPI (module API, livré plus tard) :

    await asyncio.to_thread(run_migrations)
"""

from __future__ import annotations

import logging
from pathlib import Path

from alembic import command
from alembic.config import Config

logger = logging.getLogger(__name__)

_APP_ROOT = Path(__file__).resolve().parents[1]


def run_migrations() -> None:
    """`alembic upgrade head` sur la base configurée par l'environnement."""
    cfg = Config(str(_APP_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(_APP_ROOT / "alembic"))
    logger.info("Application des migrations Alembic (upgrade head)")
    command.upgrade(cfg, "head")
    logger.info("Migrations à jour")
