"""Cas limites : métadonnées, devise au niveau service, purge 24 h, robustesse du worker."""

import asyncio
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.integration.conftest import bearer, idem, intent_payload
from tests.support import MerchantFactory
from xaalis.idempotency.models import IdempotencyRecord
from xaalis.idempotency.service import purge_expired
from xaalis.payments.service import Scope, UnsupportedCurrencyError, create_intent
from xaalis.worker.jobs import periodic


@pytest.mark.parametrize(
    "metadata",
    [{"": "x"}, {"k" * 41: "x"}, {"k": "v" * 501}, {f"k{i}": "v" for i in range(21)}],
    ids=["empty-key", "long-key", "long-value", "too-many"],
)
async def test_metadata_is_bounded(
    client: httpx.AsyncClient, onboarded_merchant: dict[str, Any], metadata: dict[str, str]
) -> None:
    response = await client.post(
        "/v1/payment-intents",
        json=intent_payload(metadata=metadata),
        headers={**bearer(onboarded_merchant["secret_key"]), **idem()},
    )
    assert response.status_code == 422


async def test_xg02_service_refuses_other_currencies_even_without_the_api(
    sessionmaker: async_sessionmaker[AsyncSession], make_merchant: MerchantFactory
) -> None:
    merchant_id = await make_merchant()
    async with sessionmaker() as session:
        with pytest.raises(UnsupportedCurrencyError):
            await create_intent(
                session,
                Scope(merchant_id, False),
                amount=1_000,
                currency="EUR",
                payer_phone="+221771234567",
                actor="test",
            )


async def test_idempotency_keys_older_than_24_hours_are_purged(
    client: httpx.AsyncClient,
    onboarded_merchant: dict[str, Any],
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    sk = bearer(onboarded_merchant["secret_key"])
    for _ in range(2):
        await client.post("/v1/payment-intents", json=intent_payload(), headers={**sk, **idem()})
    async with sessionmaker.begin() as session:
        await session.execute(
            text(
                "UPDATE idempotency_keys SET expires_at = now() - interval '1 second' "
                "WHERE merchant_id = :m"
            ),
            {"m": onboarded_merchant["id"]},
        )
    async with sessionmaker.begin() as session:
        assert await purge_expired(session) >= 2
    async with sessionmaker() as session:
        left = await session.scalar(
            select(func.count()).where(IdempotencyRecord.merchant_id == onboarded_merchant["id"])
        )
    assert left == 0


async def test_periodic_job_survives_a_failing_batch() -> None:
    calls = 0
    recovered = asyncio.Event()

    async def flaky() -> int:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("panne transitoire")
        if calls >= 3:
            recovered.set()
        return 1

    stop = asyncio.Event()
    task = asyncio.create_task(periodic("flaky", 0, flaky)(stop))
    await asyncio.wait_for(recovered.wait(), timeout=2)
    stop.set()
    await asyncio.wait_for(task, timeout=1)
    assert calls >= 3
