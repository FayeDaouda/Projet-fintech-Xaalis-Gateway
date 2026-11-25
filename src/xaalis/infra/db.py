"""Accès PostgreSQL : moteur async (asyncpg), fabrique de sessions, base déclarative.

Niveau d'isolation : READ COMMITTED (défaut PostgreSQL) + verrous explicites, cf.
``docs/adr/0002-concurrence-et-verrouillage.md``. ``lock_timeout`` et
``statement_timeout`` sont fixés au niveau de la connexion pour qu'une requête
bloquée échoue vite au lieu d'empiler les connexions du pool.
"""

from __future__ import annotations

from sqlalchemy import MetaData
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from xaalis.config import Settings

# Noms de contraintes déterministes : les migrations Alembic restent lisibles et stables.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def create_engine(settings: Settings) -> AsyncEngine:
    return create_async_engine(
        settings.database_url,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_pre_ping=True,
        connect_args={
            "server_settings": {
                "application_name": settings.service_name,
                "statement_timeout": str(settings.db_statement_timeout_ms),
                "lock_timeout": str(settings.db_lock_timeout_ms),
                "timezone": "UTC",
            }
        },
    )


def create_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
