"""Environnement Alembic (asyncpg).

L'URL de connexion vient exclusivement de transcoder.config (variables
d'environnement / .env) — jamais d'URL en dur dans alembic.ini.

Peut être exécuté :
- via la CLI `alembic upgrade head` (hors boucle asyncio) ;
- via transcoder.migrate.run_migrations(), appelé au démarrage de l'app
  dans un thread dédié (asyncio.to_thread) car env.py fait son propre
  asyncio.run().
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from transcoder.config import get_settings
from transcoder.models import Base

config = context.config

# La config de logs d'alembic.ini ne s'applique qu'à la CLI : appliquée au
# démarrage de l'app, elle remettait le logger racine en WARN et désactivait
# les loggers existants — tous les logs applicatifs (jobs, validations…)
# disparaissaient de `docker logs` après la migration.
if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _database_url() -> str:
    return get_settings().database_url


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def _run_async_migrations() -> None:
    section = config.get_section(config.config_ini_section) or {}
    section["sqlalchemy.url"] = _database_url()
    connectable = async_engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(_do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(_run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
