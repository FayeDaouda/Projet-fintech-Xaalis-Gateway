"""Webhooks marchands (lot 5) : enregistrement, diffusion, signature, relances, dead letter,
rejeu. Le récepteur est le VRAI code du marchand de démo (test de contrat)."""

import random
from typing import Any

import httpx
import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.integration.conftest import bearer, succeeded_intent
from xaalis.config import Settings
from xaalis.webhooks.delivery import WebhookDeliverer
from xaalis.webhooks.models import WebhookDelivery, WebhookEvent
from xaalis.webhooks.service import fan_out
from xaalis_sim.merchant_demo.app import DemoSettings
from xaalis_sim.merchant_demo.app import create_app as create_merchant_demo

ENDPOINTS = "/v1/webhook-endpoints"


@pytest.fixture
def secret_key(onboarded_merchant: dict[str, Any]) -> str:
    value: str = onboarded_merchant["secret_key"]
    return value


async def _register(
    client: httpx.AsyncClient, key: str, url: str, events: list[str] | None = None
) -> httpx.Response:
    return await client.post(
        ENDPOINTS,
        json={"url": url, "events": events or ["payment.succeeded", "refund.succeeded"]},
        headers=bearer(key),
    )


class Receiver:
    """Le marchand de démo, branché en ASGI ; peut être mis en panne."""

    def __init__(self, secret: str) -> None:
        self.app = create_merchant_demo(DemoSettings(demo_webhook_secret=secret))
        self.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app))
        self.bodies: list[bytes] = []

    async def events(self) -> Any:
        return (await self.http.get("http://merchant/events")).json()

    async def fail(self, enabled: bool) -> None:
        await self.http.post("http://merchant/_fail", json={"enabled": enabled})


def _deliverer(
    sessionmaker: async_sessionmaker[AsyncSession], settings: Settings, http: httpx.AsyncClient
) -> WebhookDeliverer:
    return WebhookDeliverer(
        sessionmaker,
        http,
        encryption_key=settings.webhook_encryption_key.get_secret_value(),
        schedule=[1] * 7,
        rng=random.Random(3),
    )


async def _make_due(sessionmaker: async_sessionmaker[AsyncSession], event_id: str) -> None:
    async with sessionmaker.begin() as session:
        await session.execute(
            text("UPDATE webhook_deliveries SET next_attempt_at = now() WHERE event_id = :e"),
            {"e": event_id},
        )


async def _succeeded_event(
    sessionmaker: async_sessionmaker[AsyncSession], merchant_id: str
) -> tuple[str, str]:
    """Un paiement réussi, diffusé : renvoie (intent_id, evt_ du payment.succeeded)."""
    intent_id = await succeeded_intent(sessionmaker, merchant_id, 10_000)
    async with sessionmaker.begin() as session:
        while await fan_out(session):
            pass
    async with sessionmaker() as session:
        event_id = await session.scalar(
            select(WebhookEvent.id).where(
                WebhookEvent.merchant_id == merchant_id, WebhookEvent.type == "payment.succeeded"
            )
        )
    assert event_id is not None
    return intent_id, event_id


# ------------------------------------------------------------------ enregistrement


async def test_endpoint_creation_returns_its_secret_once(
    client: httpx.AsyncClient, secret_key: str
) -> None:
    response = await _register(client, secret_key, "http://merchant-demo:8200/webhooks")
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["id"].startswith("we_")
    assert body["secret"].startswith("whsec_")
    assert body["events"] == ["payment.succeeded", "refund.succeeded"]


@pytest.mark.parametrize(
    "url",
    [
        "https://10.0.0.8/hook",
        "https://localhost/hook",
        "http://boutique.sn/hook",
        "https://169.254.169.254/latest",
    ],
)
async def test_api7_private_urls_are_refused_in_live(
    client: httpx.AsyncClient, onboarded_merchant: dict[str, Any], url: str
) -> None:
    response = await _register(client, onboarded_merchant["live_keys"]["secret_key"], url)
    assert response.status_code == 422
    assert response.json()["code"] == "unsafe_webhook_url"


async def test_unknown_event_type_is_rejected(client: httpx.AsyncClient, secret_key: str) -> None:
    response = await _register(client, secret_key, "https://boutique.sn/h", ["payment.hacked"])
    assert response.status_code == 422


# --------------------------------------------------------------- diffusion et envoi


async def test_only_subscribed_events_are_delivered_and_signature_verifies(
    client: httpx.AsyncClient,
    secret_key: str,
    onboarded_merchant: dict[str, Any],
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> None:
    endpoint = (await _register(client, secret_key, "http://merchant/webhooks")).json()
    receiver = Receiver(endpoint["secret"])
    _, event_id = await _succeeded_event(sessionmaker, onboarded_merchant["id"])

    async with sessionmaker() as session:
        types = set(
            (
                await session.scalars(
                    select(WebhookEvent.type)
                    .join(WebhookDelivery, WebhookDelivery.event_id == WebhookEvent.id)
                    .where(WebhookDelivery.endpoint_id == endpoint["id"])
                )
            ).all()
        )
    assert types == {"payment.succeeded"}, "payment.created/processing non abonnés"

    await _deliverer(sessionmaker, settings, receiver.http).run_batch()
    received = await receiver.events()
    assert [e["id"] for e in received["events"]] == [event_id]

    event = (await client.get(f"/v1/webhook-events/{event_id}", headers=bearer(secret_key))).json()
    assert event["deliveries"][0]["status"] == "delivered"
    assert event["data"]["object"]["status"] == "succeeded"


async def test_failures_are_retried_then_dead_lettered_after_eight_attempts_then_replayed(
    client: httpx.AsyncClient,
    secret_key: str,
    onboarded_merchant: dict[str, Any],
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> None:
    endpoint = (await _register(client, secret_key, "http://merchant/webhooks")).json()
    receiver = Receiver(endpoint["secret"])
    await receiver.fail(True)
    _, event_id = await _succeeded_event(sessionmaker, onboarded_merchant["id"])
    deliverer = _deliverer(sessionmaker, settings, receiver.http)

    for attempt in range(1, 9):
        await _make_due(sessionmaker, event_id)
        await deliverer.run_batch()
        state = (
            await client.get(f"/v1/webhook-events/{event_id}", headers=bearer(secret_key))
        ).json()["deliveries"][0]
        assert state["attempts"] == attempt
        assert state["last_status_code"] == 503
        assert state["status"] == ("dead_letter" if attempt == 8 else "pending")

    await _make_due(sessionmaker, event_id)
    await deliverer.run_batch()  # rien ne part depuis la dead letter sans rejeu
    assert (await receiver.events())["count"] == 0

    await receiver.fail(False)
    replay = await client.post(f"/v1/webhook-events/{event_id}/replay", headers=bearer(secret_key))
    assert replay.status_code == 202
    assert replay.json()["deliveries_rescheduled"] == 1
    await deliverer.run_batch()
    assert (await receiver.events())["count"] == 1
    state = (await client.get(f"/v1/webhook-events/{event_id}", headers=bearer(secret_key))).json()
    assert state["deliveries"][0]["status"] == "delivered"


async def test_every_attempt_sends_the_same_bytes_with_a_fresh_signature(
    client: httpx.AsyncClient,
    secret_key: str,
    onboarded_merchant: dict[str, Any],
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> None:
    await _register(client, secret_key, "http://merchant/webhooks")
    captured: list[httpx.Request] = []

    def failing(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(500)

    http = httpx.AsyncClient(transport=httpx.MockTransport(failing))
    _, event_id = await _succeeded_event(sessionmaker, onboarded_merchant["id"])
    deliverer = _deliverer(sessionmaker, settings, http)
    for _ in range(2):
        await _make_due(sessionmaker, event_id)
        await deliverer.run_batch()
    mine = [r for r in captured if r.headers["X-Xaalis-Event-Id"] == event_id]
    assert len(mine) == 2
    assert mine[0].content == mine[1].content
    assert mine[0].headers["X-Xaalis-Event-Type"] == "payment.succeeded"
    assert mine[0].headers["X-Xaalis-Signature"].startswith("t=")


async def test_live_delivery_is_blocked_when_dns_resolves_to_a_private_address(
    client: httpx.AsyncClient,
    onboarded_merchant: dict[str, Any],
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> None:
    live_key = onboarded_merchant["live_keys"]["secret_key"]
    assert (await _register(client, live_key, "https://hooks.boutique.sn/x")).status_code == 201
    calls: list[httpx.Request] = []
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: calls.append(r) or httpx.Response(200))
    )

    async def rebinding(_: str, __: int) -> list[str]:
        return ["10.1.2.3"]

    intent_id = await succeeded_intent(
        sessionmaker, onboarded_merchant["id"], 10_000, livemode=True
    )
    async with sessionmaker.begin() as session:
        while await fan_out(session):
            pass
    deliverer = WebhookDeliverer(
        sessionmaker,
        http,
        encryption_key=settings.webhook_encryption_key.get_secret_value(),
        schedule=[1] * 7,
        resolver=rebinding,
    )
    await deliverer.run_batch()
    assert not [c for c in calls if c.url.host == "hooks.boutique.sn"], "aucune requête émise"
    async with sessionmaker() as session:
        error = await session.scalar(
            select(WebhookDelivery.last_error)
            .join(WebhookEvent, WebhookEvent.id == WebhookDelivery.event_id)
            .where(WebhookEvent.payload["id"].astext == intent_id)
        )
    assert error is not None
    assert "non publique" in error


async def test_bola_replay_of_another_merchants_event_is_404(
    client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    secret_key: str,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    other = (
        await client.post("/v1/admin/merchants", json={"name": "Autre"}, headers=admin_headers)
    ).json()
    await _register(client, other["secret_key"], "http://merchant/webhooks")
    _, foreign_event = await _succeeded_event(sessionmaker, other["id"])
    for method, path in (("POST", "/replay"), ("GET", "")):
        response = await client.request(
            method, f"/v1/webhook-events/{foreign_event}{path}", headers=bearer(secret_key)
        )
        assert response.status_code == 404


async def test_unknown_event_is_404(client: httpx.AsyncClient, secret_key: str) -> None:
    from xaalis.core.ids import new_id

    response = await client.post(
        f"/v1/webhook-events/{new_id('evt')}/replay", headers=bearer(secret_key)
    )
    assert response.status_code == 404
