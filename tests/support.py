"""Utilitaires partagés par les tests (pas des fixtures : de simples fonctions et types)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from sqlalchemy.exc import DBAPIError

MerchantFactory = Callable[[], Awaitable[str]]


def sqlstate(error: DBAPIError) -> str | None:
    """Code SQLSTATE PostgreSQL d'une erreur remontée par asyncpg via SQLAlchemy."""
    return getattr(error.orig, "sqlstate", None) or getattr(error.orig, "pgcode", None)
