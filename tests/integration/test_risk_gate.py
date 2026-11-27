"""Bonus gRPC (ADR 0013) de bout en bout : API → outbox → barrière anti-fraude → opérateur.

Le dispatcher réel, la base et Redis réels, le simulateur d'opérateurs en mémoire ; le
scoring est un double de test (le transport gRPC est couvert par ``test_risk_grpc``).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.integration.conftest import bearer, idem, intent_payload, random_phone
from tests.integration.test_operator_flow import Platform, _platform
from xaalis.api.resources import Resources
from xaalis.operators.client import OperatorClient
from xaalis.operators.dispatcher import Dispatcher
from xaalis.outbox.models import OutboxMessage
from xaalis.payments.models import PaymentEvent, PaymentIntent
from xaalis.risk.gate import RiskDecision, RiskQuery, RiskSource, RiskVerdict

URL = "/v1/payment-intents"


class ScriptedRisk:
    """Double du service de scoring : renvoie le verdict prévu, enregistre les demandes."""

    def __init__(self, verdict: RiskVerdict) -> None:
        self.verdict = verdict
        self.queries: list[RiskQuery] = []

    async def score(self, query: RiskQuery) -> RiskVerdict:
        self.queries.append(query)
        return self.verdict


@pytest.fixture
async def platform(app: FastAPI, resources: Resources) -> AsyncIterator[Platform]:
    async for item in _platform(app, resources):
        yield item


def _dispatcher(platform: Platform, resources: Resources, risk: ScriptedRisk) -> Dispatcher:
    client = OperatorClient(platform.sim_http, callback_base_url="http://testserver")
    return Dispatcher(resources.sessionmaker, client, platform.health, risk=risk)


async def _confirmed(client: httpx.AsyncClient, sk: dict[str, str], amount: int) -> str:
    created = await client.post(
        URL,
        json=intent_payload(amount=amount, payer_phone=random_phone("78")),
        headers={**sk, **idem()},
    )
    intent_id: str = created.json()["id"]
    confirmed = await client.post(f"{URL}/{intent_id}/confirm", headers={**sk, **idem()})
    assert confirmed.status_code == 202, confirmed.text
    return intent_id


async def _run(dispatcher: Dispatcher, platform: Platform) -> None:
    await dispatcher.run_batch(limit=100)
    await platform.sim.drain()


@pytest.fixture
def sk(onboarded_merchant: dict[str, Any]) -> dict[str, str]:
    return bearer(onboarded_merchant["secret_key"])


async def test_declined_payment_fails_before_any_debit_request(
    client: httpx.AsyncClient,
    sk: dict[str, str],
    platform: Platform,
    resources: Resources,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    risk = ScriptedRisk(
        RiskVerdict(RiskDecision.DECLINE, RiskSource.SERVICE, 1_000, ("payer_blocklisted",))
    )
    intent_id = await _confirmed(client, sk, 25_000)

    await _run(_dispatcher(platform, resources, risk), platform)

    body = (await client.get(f"{URL}/{intent_id}", headers=sk)).json()
    assert body["status"] == "failed"
    assert body["risk"] == {"decision": "decline", "score_permille": 1000, "source": "service"}
    assert sum(platform.transport.calls.values()) == 0  # aucune demande de débit envoyée
    async with sessionmaker() as session:
        events = (
            await session.scalars(  # ORDER BY explicite : sinon aucun ordre garanti
                select(PaymentEvent)
                .where(PaymentEvent.intent_id == intent_id)
                .order_by(PaymentEvent.id)
            )
        ).all()
        topics = (
            await session.scalars(
                select(OutboxMessage.topic).where(OutboxMessage.aggregate_id == intent_id)
            )
        ).all()
    last = events[-1]
    assert (last.from_status, last.to_status, last.cause) == (
        "processing",
        "failed",
        "risk_declined",
    )
    assert last.details["reasons"] == ["payer_blocklisted"]
    assert "payment.failed" in topics  # le marchand est prévenu par webhook


async def test_approved_payment_is_assessed_once_then_charged(
    client: httpx.AsyncClient,
    sk: dict[str, str],
    platform: Platform,
    resources: Resources,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    risk = ScriptedRisk(
        RiskVerdict(RiskDecision.REVIEW, RiskSource.SERVICE, 450, ("velocity_high",))
    )
    intent_id = await _confirmed(client, sk, 12_345)

    await _run(_dispatcher(platform, resources, risk), platform)

    body = (await client.get(f"{URL}/{intent_id}", headers=sk)).json()
    assert body["status"] == "succeeded"
    assert body["risk"] == {"decision": "review", "score_permille": 450, "source": "service"}
    assert [q.intent_id for q in risk.queries] == [intent_id]
    assert risk.queries[0].amount == 12_345
    async with sessionmaker() as session:
        causes = (
            await session.scalars(
                select(PaymentEvent.cause).where(PaymentEvent.intent_id == intent_id)
            )
        ).all()
    assert causes.count("risk_assessed") == 1


async def test_held_payment_waits_without_any_debit_then_proceeds_when_scoring_is_back(
    client: httpx.AsyncClient,
    sk: dict[str, str],
    platform: Platform,
    resources: Resources,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    risk = ScriptedRisk(
        RiskVerdict(None, RiskSource.FALLBACK, None, ("risk_unavailable", "held_above_ceiling"))
    )
    intent_id = await _confirmed(client, sk, 250_000)
    dispatcher = _dispatcher(platform, resources, risk)

    await _run(dispatcher, platform)

    async with sessionmaker() as session:
        intent = await session.get(PaymentIntent, intent_id)
        message = await session.scalar(
            select(OutboxMessage).where(
                OutboxMessage.aggregate_id == intent_id,
                OutboxMessage.topic == "internal.operator.charge",
            )
        )
    assert intent is not None
    assert message is not None
    assert (intent.status, intent.risk_decision) == ("processing", None)
    assert message.published_at is None  # replanifié, pas abandonné
    assert "scoring" in (message.last_error or "")
    assert sum(platform.transport.calls.values()) == 0

    # Le service de scoring revient : la reprise évalue puis débite normalement.
    risk.verdict = RiskVerdict(RiskDecision.APPROVE, RiskSource.SERVICE, 80, ("amount_elevated",))
    async with sessionmaker.begin() as session:
        await session.execute(
            update(OutboxMessage)
            .where(OutboxMessage.id == message.id)
            .values(available_at=func.now())
        )
    await _run(dispatcher, platform)

    body = (await client.get(f"{URL}/{intent_id}", headers=sk)).json()
    assert body["status"] == "succeeded"
    assert body["risk"]["decision"] == "approve"
