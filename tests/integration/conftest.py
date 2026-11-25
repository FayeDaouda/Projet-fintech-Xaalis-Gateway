"""Fixtures d'intégration : sessions sur la vraie base, fabrique de marchands."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.support import MerchantFactory
from xaalis.admin.tokens import issue_admin_token
from xaalis.api.resources import Resources
from xaalis.config import Settings
from xaalis.merchants.service import create_merchant


@pytest.fixture(scope="session")
def sessionmaker(resources: Resources) -> async_sessionmaker[AsyncSession]:
    return resources.sessionmaker


@pytest.fixture(scope="session")
def make_merchant(sessionmaker: async_sessionmaker[AsyncSession]) -> MerchantFactory:
    """Chaque test crée ses propres marchands : les tests restent indépendants
    sans avoir à vider le grand livre, qui est en ajout seul par construction."""

    async def factory() -> str:
        async with sessionmaker.begin() as session:
            merchant = await create_merchant(session, "Boutique de test")
        return merchant.id

    return factory


@pytest.fixture(scope="session")
def admin_headers(settings: Settings) -> dict[str, str]:
    return {"Authorization": f"Bearer {issue_admin_token(settings, 'adm_test')}"}


@pytest.fixture
async def onboarded_merchant(
    client: httpx.AsyncClient, admin_headers: dict[str, str]
) -> dict[str, Any]:
    """Marchand créé par l'API d'administration, avec ses clés en clair."""
    response = await client.post(
        "/v1/admin/merchants", json={"name": "Boutique Sandaga"}, headers=admin_headers
    )
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


def bearer(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def random_phone(prefix: str = "77") -> str:
    """Numéro aléatoire : chaque test a son propre payeur (limites XG-13 et XG-14)."""
    import secrets

    return f"+221{prefix}{secrets.randbelow(10**7):07d}"


def intent_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "amount": 12_345,
        "currency": "XOF",
        "payer_phone": random_phone(),
        "metadata": {"order_id": "CMD-2026-0042"},
    }
    payload.update(overrides)
    return payload


def idem(key: str | None = None) -> dict[str, str]:
    import uuid

    return {"Idempotency-Key": key or f"test-{uuid.uuid4()}"}
