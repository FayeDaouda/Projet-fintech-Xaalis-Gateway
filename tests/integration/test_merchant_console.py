"""Console du marchand de démo branchée sur la vraie passerelle (PostgreSQL, Redis réels).

Prouve de bout en bout que ce que fait la console est accepté par le contrat : intégration
du marchand par l'API d'administration, création et confirmation idempotentes, reporting
GraphQL, remboursement signé HMAC, flux SSE relayé avec la clé publique.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import FastAPI
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.integration.conftest import random_phone, succeeded_intent
from xaalis.admin.service import create_admin, find_admin
from xaalis.payments.service import Scope, cancel_intent
from xaalis_sim.merchant_demo.app import DemoSettings, create_app

ADMIN_EMAIL = "console-test@xaalis.example"
ADMIN_PASSWORD = "console-test-password-0001"


@pytest.fixture
async def console(
    app: FastAPI, sessionmaker: async_sessionmaker[AsyncSession]
) -> AsyncIterator[httpx.AsyncClient]:
    # Administrateur propre à ces tests (le marchand du seed n'est pas touché).
    async with sessionmaker.begin() as db:
        if await find_admin(db, ADMIN_EMAIL) is None:
            await create_admin(db, ADMIN_EMAIL, ADMIN_PASSWORD)
    demo = create_app(
        DemoSettings(
            seed_admin_email=ADMIN_EMAIL,
            seed_admin_password=SecretStr(ADMIN_PASSWORD),
            merchant_demo_url="http://merchant-demo:8200/webhooks",
        ),
        api_transport=httpx.ASGITransport(app=app),
    )
    transport = httpx.ASGITransport(app=demo)
    async with httpx.AsyncClient(transport=transport, base_url="http://console") as http:
        yield http


async def test_checkout_reporting_and_signed_refund_through_the_console(
    console: httpx.AsyncClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    session = (await console.get("/api/session")).json()
    assert session["publishable_key"].startswith("pk_test_")
    assert "secret_key" not in session

    created = await console.post(
        "/api/checkout",
        json={"amount": 10_000, "payer_phone": random_phone("78"), "order_id": "CMD-1"},
        headers={"Idempotency-Key": "console-checkout-1"},
    )
    assert created.status_code == 201, created.text
    intent = created.json()
    replay = await console.post(
        "/api/checkout",
        json={"amount": 10_000, "payer_phone": "+221780000000", "order_id": "CMD-1"},
        headers={"Idempotency-Key": "console-checkout-1"},
    )
    assert replay.status_code == 422  # même clé, autre corps : refus du contrat (pas de doublon)

    confirmed = await console.post(f"/api/payment-intents/{intent['id']}/confirm")
    assert confirmed.status_code == 202, confirmed.text
    assert confirmed.json()["status"] == "processing"

    page = (await console.get("/api/payments", params={"status": "processing"})).json()
    assert intent["id"] in {node["id"] for node in page["payment_intents"]["nodes"]}

    paid = await succeeded_intent(sessionmaker, session["merchant_id"], 10_000)
    detail = (await console.get(f"/api/payment-intents/{paid}")).json()
    assert detail["status"] == "succeeded"
    assert [h["to_status"] for h in detail["history"]][-1] == "succeeded"

    refund = await console.post(f"/api/payment-intents/{paid}/refunds", json={"amount": 3_000})
    assert refund.status_code == 201, refund.text  # signature HMAC acceptée par la passerelle
    too_much = await console.post(f"/api/payment-intents/{paid}/refunds", json={"amount": 9_000})
    assert too_much.status_code == 409
    assert too_much.headers["content-type"].startswith("application/problem+json")

    overview = (await console.get("/api/overview")).json()
    assert overview["revenue"]["gross"] >= 10_000
    assert overview["revenue"]["refunded"] >= 3_000
    assert overview["balance"]["pending"] > 0


async def test_sse_stream_is_relayed_with_the_publishable_key(
    console: httpx.AsyncClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    session = (await console.get("/api/session")).json()
    created = await console.post(
        "/api/checkout", json={"amount": 5_000, "payer_phone": random_phone("76")}
    )
    intent_id = created.json()["id"]
    async with sessionmaker.begin() as db:  # état final : la passerelle ferme alors le flux
        await cancel_intent(db, Scope(session["merchant_id"], False), intent_id, actor="test")

    response = await console.get(f"/api/payment-intents/{intent_id}/stream")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.text.count("event: status_changed") == 2
    assert '"status": "canceled"' in response.text


async def test_unknown_intent_is_a_404_problem(console: httpx.AsyncClient) -> None:
    response = await console.get("/api/payment-intents/pi_01JD8Q3W7Z4K2N6R5T9V0XBCYA")

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/problem+json")
