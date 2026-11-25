"""Jeu de données de démonstration, idempotent : ``python -m xaalis.seed``.

Crée, s'ils n'existent pas : le compte administrateur et le marchand « Boutique Démo
Dakar » avec ses clés de test. Aucune clé n'est versionnée dans le dépôt : elles sont
lues depuis l'environnement (``XAALIS_DEMO_TEST_*``) ou générées au premier lancement
et affichées une seule fois dans les logs du service ``migrate``.
"""

from __future__ import annotations

import asyncio
from typing import Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from xaalis.admin.service import create_admin, find_admin
from xaalis.config import Settings, get_settings
from xaalis.infra.db import create_engine, create_sessionmaker
from xaalis.merchants.keys import KeyKind
from xaalis.merchants.models import Merchant
from xaalis.merchants.service import create_merchant, issue_key
from xaalis.observability.logging import configure_logging, get_logger

DEMO_MERCHANT_NAME: Final = "Boutique Démo Dakar"
_log = get_logger("xaalis.seed")


async def seed(sessionmaker: async_sessionmaker[AsyncSession], settings: Settings) -> None:
    async with sessionmaker.begin() as session:
        await _seed_admin(session, settings)
    async with sessionmaker.begin() as session:
        await _seed_demo_merchant(session, settings)


async def _seed_admin(session: AsyncSession, settings: Settings) -> None:
    if await find_admin(session, settings.seed_admin_email) is not None:
        _log.info("seed_admin_exists", email=settings.seed_admin_email)
        return
    await create_admin(
        session, settings.seed_admin_email, settings.seed_admin_password.get_secret_value()
    )
    _log.info("seed_admin_created", email=settings.seed_admin_email)


async def _seed_demo_merchant(session: AsyncSession, settings: Settings) -> None:
    existing = await session.scalar(select(Merchant).where(Merchant.name == DEMO_MERCHANT_NAME))
    if existing is not None:
        _log.info("seed_demo_merchant_exists", merchant_id=existing.id)
        return

    pepper = settings.api_key_pepper.get_secret_value()
    merchant = await create_merchant(session, DEMO_MERCHANT_NAME)
    issued: dict[str, str] = {}
    for kind, provided in (
        (KeyKind.PUBLISHABLE, settings.demo_test_publishable_key),
        (KeyKind.SECRET, settings.demo_test_secret_key),
    ):
        _, raw = await issue_key(
            session,
            merchant.id,
            kind=kind,
            livemode=False,
            pepper=pepper,
            raw_key=provided.get_secret_value() if provided else None,
        )
        issued[kind.value] = raw
    # Affichage unique, en environnement de développement seulement.
    _log.warning(
        "seed_demo_merchant_created",
        merchant_id=merchant.id,
        publishable_key=issued[KeyKind.PUBLISHABLE.value],
        secret_key=issued[KeyKind.SECRET.value] if settings.environment != "production" else "***",
        note="clés de TEST affichées une seule fois ; copiez-les maintenant",
    )


async def main() -> None:
    settings = get_settings()
    configure_logging(service="xaalis-seed", level="INFO", json_output=settings.log_json)
    engine = create_engine(settings)
    try:
        await seed(create_sessionmaker(engine), settings)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
