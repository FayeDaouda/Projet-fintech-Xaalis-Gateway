"""GET /v1/admin/ledger/health : authentification admin et invariant XG-16 sous charge."""

import asyncio
import random

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.integration.ledger_simulation import MerchantModel, apply_operation, random_operation
from tests.support import MerchantFactory
from xaalis.admin.tokens import issue_admin_token
from xaalis.config import Settings
from xaalis.ledger import service

HEALTH_URL = "/v1/admin/ledger/health"


async def test_health_requires_a_token(client: httpx.AsyncClient) -> None:
    response = await client.get(HEALTH_URL)
    assert response.status_code == 401
    assert response.json()["code"] == "unauthenticated"
    assert response.headers["WWW-Authenticate"].startswith("Bearer")


async def test_health_rejects_an_invalid_token(client: httpx.AsyncClient) -> None:
    response = await client.get(HEALTH_URL, headers={"Authorization": "Bearer not-a-jwt"})
    assert response.status_code == 401
    assert response.json()["code"] == "invalid_token"


async def test_health_rejects_a_non_admin_role(
    client: httpx.AsyncClient, settings: Settings
) -> None:
    token = issue_admin_token(settings, "adm_support", role="support")
    response = await client.get(HEALTH_URL, headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 403
    assert response.json()["code"] == "forbidden"


async def test_xg16_ledger_is_balanced_after_1000_random_operations(
    client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    sessionmaker: async_sessionmaker[AsyncSession],
    make_merchant: MerchantFactory,
) -> None:
    """Critère de fin de journée du planning : 1 000 opérations, 10 marchands en parallèle
    (ce qui exerce aussi la création concurrente des comptes partagés)."""
    rng = random.Random(20261125)
    models = [MerchantModel(await make_merchant()) for _ in range(10)]

    async def run(model: MerchantModel, ops_rng: random.Random) -> None:
        for _ in range(100):
            await apply_operation(sessionmaker, model, random_operation(ops_rng))

    await asyncio.gather(*(run(m, random.Random(rng.random())) for m in models))

    for model in models:
        async with sessionmaker() as session:
            actual = await service.merchant_balances(session, model.merchant_id, livemode=False)
        assert actual == model.expected()

    response = await client.get(HEALTH_URL, headers=admin_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["balanced"] is True
    assert body["totals"] == {"all": 0, "test": 0, "live": 0}
    assert body["unbalanced_transactions"] == []
    assert body["immutability_enforced"] is True
    assert body["transactions_count"] >= 500
