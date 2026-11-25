"""Le jeu de démonstration est idempotent et ses clés fonctionnent."""

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.integration.conftest import bearer
from xaalis.admin.models import AdminUser
from xaalis.config import Settings
from xaalis.merchants.keys import KeyKind, generate_key
from xaalis.merchants.models import Merchant
from xaalis.merchants.service import create_merchant, issue_key
from xaalis.seed import DEMO_MERCHANT_NAME, seed


async def test_seed_is_idempotent_and_uses_provided_demo_keys(
    client: httpx.AsyncClient,
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> None:
    secret = generate_key(KeyKind.SECRET, livemode=False)
    publishable = generate_key(KeyKind.PUBLISHABLE, livemode=False)
    seeded = settings.model_copy(
        update={
            "seed_admin_email": "seed-test@xaalis.example",
            "demo_test_secret_key": SecretStr(secret),
            "demo_test_publishable_key": SecretStr(publishable),
        }
    )

    await seed(sessionmaker, seeded)
    await seed(sessionmaker, seeded)

    async with sessionmaker() as session:
        admins = await session.scalar(
            select(func.count()).where(AdminUser.email == "seed-test@xaalis.example")
        )
        merchants = await session.scalar(
            select(func.count()).where(Merchant.name == DEMO_MERCHANT_NAME)
        )
    assert admins == 1
    assert merchants == 1
    response = await client.post("/v1/api-keys/rotate", headers=bearer(secret))
    assert response.status_code == 201


async def test_demo_key_of_the_wrong_environment_is_refused(
    sessionmaker: async_sessionmaker[AsyncSession], settings: Settings
) -> None:
    live_key = generate_key(KeyKind.SECRET, livemode=True)
    async with sessionmaker() as session:
        merchant = await create_merchant(session, "Mauvaise configuration")
        with pytest.raises(ValueError, match="environnement"):
            await issue_key(
                session,
                merchant.id,
                kind=KeyKind.SECRET,
                livemode=False,
                pepper=settings.api_key_pepper.get_secret_value(),
                raw_key=live_key,
            )
