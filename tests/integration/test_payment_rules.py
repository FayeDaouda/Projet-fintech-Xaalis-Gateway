"""XG-07 (expiration), XG-14 (3 intents en cours par payeur), XG-13 (débit), outbox."""

import asyncio
from typing import Any

import httpx
import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.integration.conftest import bearer, idem, intent_payload, random_phone
from tests.support import sqlstate
from xaalis.outbox.models import OutboxMessage
from xaalis.payments.service import expire_due_intents
from xaalis.worker.jobs import expiry_job

URL = "/v1/payment-intents"


@pytest.fixture
def sk(onboarded_merchant: dict[str, Any]) -> dict[str, str]:
    return bearer(onboarded_merchant["secret_key"])


async def _create(client: httpx.AsyncClient, sk: dict[str, str], **overrides: object) -> str:
    response = await client.post(URL, json=intent_payload(**overrides), headers={**sk, **idem()})
    assert response.status_code == 201, response.text
    intent_id: str = response.json()["id"]
    return intent_id


async def _confirm(client: httpx.AsyncClient, sk: dict[str, str], intent_id: str) -> httpx.Response:
    return await client.post(f"{URL}/{intent_id}/confirm", headers={**sk, **idem()})


async def _age(sessionmaker: async_sessionmaker[AsyncSession], intent_id: str) -> None:
    """Simule l'écoulement des 15 minutes."""
    async with sessionmaker.begin() as session:
        await session.execute(
            text(
                "UPDATE payment_intents SET expires_at = now() - interval '1 second' WHERE id = :id"
            ),
            {"id": intent_id},
        )


# --------------------------------------------------------------------------- XG-07


@pytest.mark.parametrize("confirm_first", [False, True], ids=["created", "processing"])
async def test_xg07_intent_without_outcome_expires_after_15_minutes(
    client: httpx.AsyncClient,
    sk: dict[str, str],
    sessionmaker: async_sessionmaker[AsyncSession],
    confirm_first: bool,
) -> None:
    intent_id = await _create(client, sk)
    if confirm_first:
        await _confirm(client, sk, intent_id)
    await _age(sessionmaker, intent_id)

    async with sessionmaker.begin() as session:
        expired = await expire_due_intents(session)
    assert intent_id in expired
    body = (await client.get(f"{URL}/{intent_id}", headers=sk)).json()
    assert body["status"] == "expired"


async def test_xg07_intent_not_yet_due_is_left_alone(
    client: httpx.AsyncClient, sk: dict[str, str], sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    intent_id = await _create(client, sk)
    async with sessionmaker.begin() as session:
        assert intent_id not in await expire_due_intents(session)


async def test_xg07_overdue_intent_cannot_be_confirmed_even_before_the_job_runs(
    client: httpx.AsyncClient, sk: dict[str, str], sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    intent_id = await _create(client, sk)
    await _age(sessionmaker, intent_id)
    response = await _confirm(client, sk, intent_id)
    assert response.status_code == 409
    assert response.json()["code"] == "intent_expired"


async def test_xg07_worker_job_expires_intents_then_stops_cleanly(
    client: httpx.AsyncClient, sk: dict[str, str], sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    intent_id = await _create(client, sk)
    await _age(sessionmaker, intent_id)
    stop = asyncio.Event()
    task = asyncio.create_task(expiry_job(sessionmaker, interval_seconds=1)(stop))
    for _ in range(50):
        if (await client.get(f"{URL}/{intent_id}", headers=sk)).json()["status"] == "expired":
            break
        await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(task, timeout=3)
    assert (await client.get(f"{URL}/{intent_id}", headers=sk)).json()["status"] == "expired"


# --------------------------------------------------------------------------- XG-14


async def test_xg14_fourth_concurrent_processing_intent_is_refused(
    client: httpx.AsyncClient, sk: dict[str, str]
) -> None:
    phone = random_phone()
    ids = [await _create(client, sk, payer_phone=phone) for _ in range(4)]
    for intent_id in ids[:3]:
        assert (await _confirm(client, sk, intent_id)).status_code == 202
    refused = await _confirm(client, sk, ids[3])
    assert refused.status_code == 409
    assert refused.json()["code"] == "payer_processing_limit"


async def test_xg14_limit_holds_under_concurrent_confirmations(
    client: httpx.AsyncClient, sk: dict[str, str]
) -> None:
    phone = random_phone()
    ids = [await _create(client, sk, payer_phone=phone) for _ in range(10)]
    responses = await asyncio.gather(*(_confirm(client, sk, intent_id) for intent_id in ids))
    statuses = sorted(r.status_code for r in responses)
    assert statuses.count(202) == 3
    assert statuses.count(409) == 7


async def test_rejected_confirmation_publishes_nothing(
    client: httpx.AsyncClient, sk: dict[str, str], sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """Outbox transactionnel : une transaction annulée n'émet aucun événement."""
    phone = random_phone()
    ids = [await _create(client, sk, payer_phone=phone) for _ in range(4)]
    for intent_id in ids[:3]:
        await _confirm(client, sk, intent_id)
    assert (await _confirm(client, sk, ids[3])).status_code == 409
    async with sessionmaker() as session:
        topics = (
            await session.scalars(
                select(OutboxMessage.topic).where(OutboxMessage.aggregate_id == ids[3])
            )
        ).all()
    assert list(topics) == ["payment.created"]


# --------------------------------------------------------------------------- XG-13


async def test_xg13_twenty_one_creations_per_minute_for_one_payer_returns_429(
    client: httpx.AsyncClient, sk: dict[str, str]
) -> None:
    phone = random_phone()
    for _ in range(20):
        await _create(client, sk, payer_phone=phone)
    response = await client.post(
        URL, json=intent_payload(payer_phone=phone), headers={**sk, **idem()}
    )
    assert response.status_code == 429
    assert response.json()["code"] == "rate_limited"
    assert int(response.headers["Retry-After"]) >= 1


async def test_xg13_hundred_requests_per_minute_per_key(
    client: httpx.AsyncClient, sk: dict[str, str]
) -> None:
    statuses = [(await client.get(URL, headers=sk)).status_code for _ in range(100)]
    assert set(statuses) == {200}
    blocked = await client.get(URL, headers=sk)
    assert blocked.status_code == 429
    assert "Retry-After" in blocked.headers


# --------------------------------------------------------- contraintes en base


async def test_xg09_database_refuses_refunding_more_than_the_amount(
    client: httpx.AsyncClient, sk: dict[str, str], sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    intent_id = await _create(client, sk)
    with pytest.raises(DBAPIError) as excinfo:
        async with sessionmaker.begin() as session:
            await session.execute(
                text("UPDATE payment_intents SET amount_refunded = amount + 1 WHERE id = :id"),
                {"id": intent_id},
            )
    assert sqlstate(excinfo.value) == "23514"


async def test_payment_history_is_append_only(
    client: httpx.AsyncClient, sk: dict[str, str], sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    intent_id = await _create(client, sk)
    with pytest.raises(DBAPIError) as excinfo:
        async with sessionmaker.begin() as session:
            await session.execute(
                text("UPDATE payment_events SET to_status = 'succeeded' WHERE intent_id = :id"),
                {"id": intent_id},
            )
    assert sqlstate(excinfo.value) == "XA001"
