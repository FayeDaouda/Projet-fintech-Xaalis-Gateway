"""Remboursements : XG-08 (état, 30 jours), XG-09 (jamais plus que le montant, même en
concurrence), XG-10 (commission non remboursée), XG-11 (solde suffisant)."""

import asyncio
import json
from typing import Any

import httpx
import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.integration.conftest import (
    bearer,
    idem,
    intent_payload,
    signed_headers,
    succeeded_intent,
)
from xaalis.ledger.accounts import AccountRef
from xaalis.ledger.health import check_ledger_health
from xaalis.ledger.service import account_balance
from xaalis.outbox.models import OutboxMessage

URL = "/v1/refunds"


async def _refund(
    client: httpx.AsyncClient,
    secret: str,
    intent_id: str,
    amount: int | None = None,
    *,
    idempotency_key: str | None = None,
) -> httpx.Response:
    payload: dict[str, object] = {"payment_intent": intent_id}
    if amount is not None:
        payload["amount"] = amount
    body = json.dumps(payload).encode()
    return await client.post(
        URL, content=body, headers=signed_headers(secret, body, idempotency_key=idempotency_key)
    )


async def _balance(client: httpx.AsyncClient, secret: str) -> Any:
    return (await client.get("/v1/balance", headers=bearer(secret))).json()


@pytest.fixture
def secret(onboarded_merchant: dict[str, Any]) -> str:
    key: str = onboarded_merchant["secret_key"]
    return key


@pytest.fixture
def merchant_id(onboarded_merchant: dict[str, Any]) -> str:
    value: str = onboarded_merchant["id"]
    return value


async def test_partial_then_full_refund(
    client: httpx.AsyncClient,
    secret: str,
    merchant_id: str,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    await succeeded_intent(sessionmaker, merchant_id, 50_000)  # solde de couverture
    intent_id = await succeeded_intent(sessionmaker, merchant_id, 10_000)

    partial = await _refund(client, secret, intent_id, 3_000)
    assert partial.status_code == 201, partial.text
    body = partial.json()
    assert body["id"].startswith("re_")
    assert (body["object"], body["amount"], body["status"]) == ("refund", 3_000, "succeeded")
    assert body["payment_intent"] == intent_id
    intent = (await client.get(f"/v1/payment-intents/{intent_id}", headers=bearer(secret))).json()
    assert (intent["status"], intent["amount_refunded"]) == ("partially_refunded", 3_000)

    rest = await _refund(client, secret, intent_id)  # montant omis : tout le reste
    assert rest.json()["amount"] == 7_000
    intent = (await client.get(f"/v1/payment-intents/{intent_id}", headers=bearer(secret))).json()
    assert (intent["status"], intent["amount_refunded"]) == ("refunded", 10_000)


async def test_xg10_commission_is_not_refunded(
    client: httpx.AsyncClient,
    secret: str,
    merchant_id: str,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    await succeeded_intent(sessionmaker, merchant_id, 10_000)
    intent_id = await succeeded_intent(sessionmaker, merchant_id, 10_000)
    assert (await _balance(client, secret))["pending"] == 2 * 9_820
    async with sessionmaker() as session:
        revenue_before = await account_balance(session, AccountRef.revenue(), livemode=False)

    assert (await _refund(client, secret, intent_id)).status_code == 201

    # Le payeur récupère 10 000 ; le marchand, qui n'avait reçu que 9 820, supporte les 180.
    assert (await _balance(client, secret))["pending"] == 2 * 9_820 - 10_000
    async with sessionmaker() as session:
        revenue_after = await account_balance(session, AccountRef.revenue(), livemode=False)
    assert revenue_after == revenue_before, "xaalis_revenue n'est jamais extourné"


async def test_xg11_refund_refused_when_merchant_balance_is_insufficient(
    client: httpx.AsyncClient,
    secret: str,
    merchant_id: str,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    intent_id = await succeeded_intent(sessionmaker, merchant_id, 10_000)  # solde : 9 820
    response = await _refund(client, secret, intent_id)  # 10 000 > 9 820
    assert response.status_code == 409
    assert response.json()["code"] == "insufficient_balance"
    assert (await _refund(client, secret, intent_id, 9_820)).status_code == 201


async def test_xg09_cannot_refund_more_than_the_amount(
    client: httpx.AsyncClient,
    secret: str,
    merchant_id: str,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    await succeeded_intent(sessionmaker, merchant_id, 100_000)
    intent_id = await succeeded_intent(sessionmaker, merchant_id, 10_000)
    assert (await _refund(client, secret, intent_id, 6_000)).status_code == 201
    response = await _refund(client, secret, intent_id, 4_001)
    assert response.status_code == 409
    assert response.json()["code"] == "refund_exceeds_amount"


async def test_xg09_fifty_concurrent_refunds_accept_exactly_ten(
    client: httpx.AsyncClient,
    secret: str,
    merchant_id: str,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Exigence de l'énoncé : 50 × 1 000 sur 10 000, le solde étant par ailleurs suffisant."""
    await succeeded_intent(sessionmaker, merchant_id, 200_000)  # seule XG-09 doit limiter
    intent_id = await succeeded_intent(sessionmaker, merchant_id, 10_000)

    responses = await asyncio.gather(
        *(_refund(client, secret, intent_id, 1_000) for _ in range(50))
    )

    codes = [r.status_code for r in responses]
    assert codes.count(201) == 10, codes
    assert codes.count(409) == 40
    # Après le 10e, l'intent est « refunded » (état terminal) : refus par la machine à états.
    assert {r.json()["code"] for r in responses if r.status_code == 409} <= {
        "refund_exceeds_amount",
        "invalid_transition",
    }
    intent = (await client.get(f"/v1/payment-intents/{intent_id}", headers=bearer(secret))).json()
    assert (intent["status"], intent["amount_refunded"]) == ("refunded", 10_000)
    async with sessionmaker() as session:
        assert (await check_ledger_health(session)).balanced


@pytest.mark.parametrize("confirm", [False, True], ids=["created", "processing"])
async def test_xg08_only_succeeded_intents_are_refundable(
    client: httpx.AsyncClient, secret: str, confirm: bool
) -> None:
    created = await client.post(
        "/v1/payment-intents", json=intent_payload(), headers={**bearer(secret), **idem()}
    )
    intent_id = created.json()["id"]
    if confirm:
        await client.post(
            f"/v1/payment-intents/{intent_id}/confirm", headers={**bearer(secret), **idem()}
        )
    response = await _refund(client, secret, intent_id, 1_000)
    assert response.status_code == 409
    assert response.json()["code"] == "invalid_transition"


async def test_xg08_fully_refunded_intent_cannot_be_refunded_again(
    client: httpx.AsyncClient,
    secret: str,
    merchant_id: str,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    await succeeded_intent(sessionmaker, merchant_id, 50_000)
    intent_id = await succeeded_intent(sessionmaker, merchant_id, 5_000)
    await _refund(client, secret, intent_id)
    response = await _refund(client, secret, intent_id)
    assert response.status_code == 409
    assert response.json()["code"] == "invalid_transition"


async def test_xg08_refund_window_is_thirty_days(
    client: httpx.AsyncClient,
    secret: str,
    merchant_id: str,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    await succeeded_intent(sessionmaker, merchant_id, 50_000)
    intent_id = await succeeded_intent(sessionmaker, merchant_id, 5_000)
    async with sessionmaker.begin() as session:
        await session.execute(
            text(
                "UPDATE payment_intents SET succeeded_at = now() - interval '30 days 1 minute' "
                "WHERE id = :id"
            ),
            {"id": intent_id},
        )
    response = await _refund(client, secret, intent_id, 1_000)
    assert response.status_code == 409
    assert response.json()["code"] == "refund_window_expired"


async def test_refund_requires_hmac_signature(
    client: httpx.AsyncClient,
    secret: str,
    merchant_id: str,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    intent_id = await succeeded_intent(sessionmaker, merchant_id, 5_000)
    response = await client.post(
        URL, json={"payment_intent": intent_id, "amount": 100}, headers={**bearer(secret), **idem()}
    )
    assert response.status_code == 401
    assert response.json()["code"] == "signature_required"


async def test_refund_replay_does_not_refund_twice(
    client: httpx.AsyncClient,
    secret: str,
    merchant_id: str,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    await succeeded_intent(sessionmaker, merchant_id, 50_000)
    intent_id = await succeeded_intent(sessionmaker, merchant_id, 10_000)
    first = await _refund(client, secret, intent_id, 2_000, idempotency_key="refund-cmd-1")
    replay = await _refund(client, secret, intent_id, 2_000, idempotency_key="refund-cmd-1")
    assert replay.content == first.content
    assert replay.headers["Idempotent-Replayed"] == "true"
    intent = (await client.get(f"/v1/payment-intents/{intent_id}", headers=bearer(secret))).json()
    assert intent["amount_refunded"] == 2_000


async def test_bola_cannot_refund_another_merchants_payment(
    client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    secret: str,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    other = (
        await client.post("/v1/admin/merchants", json={"name": "Autre"}, headers=admin_headers)
    ).json()
    foreign = await succeeded_intent(sessionmaker, other["id"], 10_000)
    response = await _refund(client, secret, foreign, 1_000)
    assert response.status_code == 404


async def test_refund_publishes_refund_and_payment_events(
    client: httpx.AsyncClient,
    secret: str,
    merchant_id: str,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    await succeeded_intent(sessionmaker, merchant_id, 50_000)
    intent_id = await succeeded_intent(sessionmaker, merchant_id, 10_000)
    refund_id = (await _refund(client, secret, intent_id, 1_000)).json()["id"]
    async with sessionmaker() as session:
        topics = set(
            (
                await session.scalars(
                    select(OutboxMessage.topic).where(
                        OutboxMessage.aggregate_id.in_([intent_id, refund_id])
                    )
                )
            ).all()
        )
    assert {"refund.succeeded", "payment.partially_refunded"} <= topics


async def test_balance_reports_the_three_merchant_accounts(
    client: httpx.AsyncClient,
    secret: str,
    merchant_id: str,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    await succeeded_intent(sessionmaker, merchant_id, 12_345)
    body = await _balance(client, secret)
    assert body == {
        "object": "balance",
        "livemode": False,
        "currency": "XOF",
        "pending": 12_122,
        "available": 0,
        "paid_out": 0,
    }
