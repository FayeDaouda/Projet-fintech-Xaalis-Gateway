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


async def succeeded_intent(
    sessionmaker: async_sessionmaker[AsyncSession],
    merchant_id: str,
    amount: int,
    *,
    livemode: bool = False,
) -> str:
    """Crée un intent réussi par le vrai chemin : création, confirmation, callback opérateur."""
    import uuid as _uuid

    from xaalis.core.operators import Operator
    from xaalis.operators.callbacks import CallbackOutcome, NormalizedCallback
    from xaalis.operators.service import handle_callback
    from xaalis.payments.service import Scope, confirm_intent, create_intent

    scope = Scope(merchant_id, livemode)
    async with sessionmaker.begin() as session:
        intent = await create_intent(
            session,
            scope,
            amount=amount,
            currency="XOF",
            payer_phone=random_phone("78"),
            actor="test",
        )
    async with sessionmaker.begin() as session:
        await confirm_intent(session, scope, intent.id, actor="test")
    async with sessionmaker.begin() as session:
        await handle_callback(
            session,
            NormalizedCallback(
                Operator.GOPAY,
                f"GP-{_uuid.uuid4().hex}",
                intent.id,
                CallbackOutcome.SUCCEEDED,
                amount,
                {},
            ),
        )
    return intent.id


def signed_headers(
    secret_key: str, body: bytes, *, idempotency_key: str | None = None
) -> dict[str, str]:
    """En-têtes d'une requête sensible : clé secrète, signature HMAC horodatée, idempotence."""
    import time as _time

    from xaalis.core.signatures import sign

    ts = int(_time.time())
    return {
        **bearer(secret_key),
        **idem(idempotency_key),
        "Content-Type": "application/json",
        "X-Xaalis-Timestamp": str(ts),
        "X-Xaalis-Signature": sign(secret_key, ts, body),
    }
