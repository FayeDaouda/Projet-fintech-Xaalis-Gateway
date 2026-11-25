"""Environnement Alembic (async, asyncpg).

Toutes les tables, contraintes et triggers sont créés par des migrations versionnées :
aucun ``metadata.create_all`` n'est utilisé, ni en production ni dans les tests.
"""

from __future__ import annotations

import asyncio

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from xaalis.config import get_settings
from xaalis.models import Base

config = context.config
target_metadata = Base.metadata


def _database_url() -> str:
    # Les tests passent l'URL de leur conteneur éphémère via les attributs de config.
    override = config.attributes.get("database_url")
    return str(override) if override else get_settings().database_url


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _run_sync_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        transaction_per_migration=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    engine = create_async_engine(_database_url(), poolclass=pool.NullPool)
    async with engine.connect() as connection:
        await connection.run_sync(_run_sync_migrations)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
