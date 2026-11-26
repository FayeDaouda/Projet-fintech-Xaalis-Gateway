"""Lot 3 de bout en bout : API → outbox → dispatcher → simulateur → callback → grand livre.

La passerelle et le simulateur tournent dans le même processus, reliés par de vrais
échanges HTTP (ASGI) ; la base et Redis sont réels.
"""

from __future__ import annotations

import uuid
from collections import Counter
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.integration.conftest import bearer, idem, intent_payload, random_phone
from xaalis.api.resources import Resources
from xaalis.core.operators import Operator
from xaalis.ledger.service import merchant_balances
from xaalis.operators.client import OperatorClient
from xaalis.operators.dispatcher import Dispatcher
from xaalis.operators.models import OperatorCallback, ReconciliationAlert
from xaalis.outbox.models import OutboxMessage
from xaalis.payments.models import PaymentEvent
from xaalis.payments.service import OPERATOR_CHARGE_TOPIC, expire_due_intents
from xaalis.routing.health import BreakerState, OperatorHealth
from xaalis_sim.operators import formats
from xaalis_sim.operators.app import Simulator, SimulatorSettings
from xaalis_sim.operators.app import create_app as create_simulator

URL = "/v1/payment-intents"


class CountingTransport(httpx.AsyncBaseTransport):
    """Compte les appels par opérateur ; peut « perdre » la réponse de la 1re demande de débit."""

    def __init__(self, inner: httpx.AsyncBaseTransport, *, lose_first_response: bool = False):
        self.inner = inner
        self.calls: Counter[str] = Counter()
        self._lose = lose_first_response

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.calls[request.url.path.split("/")[1]] += 1
        response = await self.inner.handle_async_request(request)
        if self._lose and request.method == "POST" and request.url.path.endswith("/charges"):
            self._lose = False
            await response.aread()
            raise httpx.ReadTimeout("réponse perdue après traitement", request=request)
        return response


@dataclass
class Platform:
    sim: Simulator
    sim_http: httpx.AsyncClient
    transport: CountingTransport
    dispatcher: Dispatcher
    health: OperatorHealth

    async def chaos(self, operator: str, **settings: Any) -> None:
        response = await self.sim_http.post("/_chaos", json={"operator": operator, **settings})
        assert response.status_code == 200, response.text

    async def run(self) -> None:
        """Un tour du worker, puis attente de tous les callbacks du simulateur."""
        await self.dispatcher.run_batch()
        await self.sim.drain()


async def _platform(
    app: FastAPI, resources: Resources, *, lose_first_response: bool = False
) -> AsyncIterator[Platform]:
    gateway = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    )
    sim_app = create_simulator(SimulatorSettings(sim_seed=7), callback_client=gateway)
    transport = CountingTransport(
        httpx.ASGITransport(app=sim_app), lose_first_response=lose_first_response
    )
    sim_http = httpx.AsyncClient(transport=transport, base_url="http://operators")
    health = OperatorHealth(
        resources.redis, failure_threshold=5, cooldown_seconds=30, namespace=f"t-{uuid.uuid4()}"
    )
    client = OperatorClient(sim_http, callback_base_url="http://testserver")
    platform = Platform(
        sim_app.state.simulator,
        sim_http,
        transport,
        Dispatcher(resources.sessionmaker, client, health),
        health,
    )
    for operator in ("gopay", "teranga", "ndiaga"):  # comportement déterministe
        await platform.chaos(operator, latency_ms=[0, 0], failure_rate=0, duplicate_rate=0)
    # Indépendance vis-à-vis de l'ordre des tests : les paiements confirmés par d'autres tests
    # (sans worker) sont traités d'abord, pour que run() ne serve que ceux de ce test.
    while await platform.dispatcher.run_batch(limit=100):
        await platform.sim.drain()
    transport.calls.clear()
    yield platform
    await sim_http.aclose()
    await gateway.aclose()


@pytest.fixture
async def platform(app: FastAPI, resources: Resources) -> AsyncIterator[Platform]:
    async for item in _platform(app, resources):
        yield item


@pytest.fixture
def sk(onboarded_merchant: dict[str, Any]) -> dict[str, str]:
    return bearer(onboarded_merchant["secret_key"])


async def _confirmed(
    client: httpx.AsyncClient, sk: dict[str, str], prefix: str, *, request_id: str | None = None
) -> str:
    created = await client.post(
        URL, json=intent_payload(payer_phone=random_phone(prefix)), headers={**sk, **idem()}
    )
    intent_id: str = created.json()["id"]
    headers = {**sk, **idem(), **({"X-Request-Id": request_id} if request_id else {})}
    confirmed = await client.post(f"{URL}/{intent_id}/confirm", headers=headers)
    assert confirmed.status_code == 202, confirmed.text
    return intent_id


async def _intent(client: httpx.AsyncClient, sk: dict[str, str], intent_id: str) -> Any:
    return (await client.get(f"{URL}/{intent_id}", headers=sk)).json()


# --------------------------------------------------------------------------- nominal


@pytest.mark.parametrize(
    ("prefix", "operator"), [("78", "gopay"), ("76", "teranga"), ("75", "ndiaga")]
)
async def test_payment_succeeds_end_to_end_and_is_booked_in_the_ledger(
    client: httpx.AsyncClient,
    sk: dict[str, str],
    platform: Platform,
    onboarded_merchant: dict[str, Any],
    sessionmaker: async_sessionmaker[AsyncSession],
    prefix: str,
    operator: str,
) -> None:
    intent_id = await _confirmed(client, sk, prefix)
    await platform.run()

    body = await _intent(client, sk, intent_id)
    assert (body["status"], body["operator"]) == ("succeeded", operator)
    assert body["succeeded_at"] is not None
    async with sessionmaker() as session:
        balances = await merchant_balances(session, onboarded_merchant["id"], livemode=False)
        topics = (
            await session.scalars(
                select(OutboxMessage.topic).where(OutboxMessage.aggregate_id == intent_id)
            )
        ).all()
    assert balances.pending == 12_122  # net : 12 345 − 223
    assert "payment.succeeded" in topics


async def test_failed_payment_books_nothing(
    client: httpx.AsyncClient,
    sk: dict[str, str],
    platform: Platform,
    onboarded_merchant: dict[str, Any],
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    await platform.chaos("gopay", failure_rate=1)
    intent_id = await _confirmed(client, sk, "78")
    await platform.run()

    assert (await _intent(client, sk, intent_id))["status"] == "failed"
    async with sessionmaker() as session:
        balances = await merchant_balances(session, onboarded_merchant["id"], livemode=False)
    assert balances.pending == 0


async def test_teranga_duplicate_callback_is_applied_once(
    client: httpx.AsyncClient,
    sk: dict[str, str],
    platform: Platform,
    onboarded_merchant: dict[str, Any],
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    await platform.chaos("teranga", duplicate_rate=1)
    intent_id = await _confirmed(client, sk, "76")
    await platform.run()

    charge = platform.sim.charges[("teranga", intent_id)]
    assert charge.callbacks_sent == 2, "le simulateur a bien envoyé deux fois"
    async with sessionmaker() as session:
        stored = await session.scalar(
            select(func.count()).where(OperatorCallback.intent_id == intent_id)
        )
        balances = await merchant_balances(session, onboarded_merchant["id"], livemode=False)
    assert stored == 1
    assert balances.pending == 12_122, "crédité une seule fois"


async def test_request_id_is_propagated_to_the_operator(
    client: httpx.AsyncClient, sk: dict[str, str], platform: Platform
) -> None:
    intent_id = await _confirmed(client, sk, "78", request_id="req_trace_e2e_0001")
    await platform.run()
    assert platform.sim.charges[("gopay", intent_id)].request_id == "req_trace_e2e_0001"


# --------------------------------------------------------------- bascule et disjoncteur


async def test_ndiaga_outage_fails_over_to_gopay_without_double_charge(
    client: httpx.AsyncClient,
    sk: dict[str, str],
    platform: Platform,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    await platform.chaos("ndiaga", outage_seconds=60)
    intent_id = await _confirmed(client, sk, "77")  # 77 : Ndiaga (0,8 %) puis Go Pay (0,9 %)
    await platform.run()

    body = await _intent(client, sk, intent_id)
    assert (body["status"], body["operator"]) == ("succeeded", "gopay")
    assert ("ndiaga", intent_id) not in platform.sim.charges, "Ndiaga n'a rien débité"
    async with sessionmaker() as session:
        causes = (
            await session.scalars(
                select(PaymentEvent.cause).where(PaymentEvent.intent_id == intent_id)
            )
        ).all()
    assert "operator_failover" in causes


async def test_breaker_opens_after_five_failures_then_ndiaga_is_skipped(
    client: httpx.AsyncClient, sk: dict[str, str], platform: Platform
) -> None:
    await platform.chaos("ndiaga", outage_seconds=60)
    first = [await _confirmed(client, sk, "77") for _ in range(5)]
    await platform.run()
    assert platform.transport.calls["ndiaga"] == 5
    assert await platform.health.state(Operator.NDIAGA) is BreakerState.OPEN

    platform.transport.calls.clear()
    sixth = await _confirmed(client, sk, "77")
    await platform.run()
    assert platform.transport.calls["ndiaga"] == 0, "disjoncteur ouvert : Ndiaga n'est plus appelé"
    for intent_id in [*first, sixth]:
        assert (await _intent(client, sk, intent_id))["operator"] == "gopay"


async def test_all_operators_down_keeps_intent_processing_and_retries_later(
    client: httpx.AsyncClient,
    sk: dict[str, str],
    platform: Platform,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    await platform.chaos("ndiaga", outage_seconds=60)
    await platform.chaos("gopay", outage_seconds=60)
    intent_id = await _confirmed(client, sk, "77")
    await platform.run()

    assert (await _intent(client, sk, intent_id))["status"] == "processing"
    async with sessionmaker() as session:
        message = await session.scalar(
            select(OutboxMessage).where(
                OutboxMessage.aggregate_id == intent_id,
                OutboxMessage.topic == OPERATOR_CHARGE_TOPIC,
            )
        )
    assert message is not None
    assert message.published_at is None
    assert message.last_error == "aucun opérateur disponible"


async def test_ambiguous_timeout_pins_the_operator_and_never_charges_twice(
    app: FastAPI,
    resources: Resources,
    client: httpx.AsyncClient,
    sk: dict[str, str],
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Ndiaga débite, mais sa réponse se perd (timeout). Basculer vers Go Pay débiterait
    le payeur deux fois : on épingle Ndiaga, son callback finit par arriver."""
    async for platform in _platform(app, resources, lose_first_response=True):
        intent_id = await _confirmed(client, sk, "77")
        await platform.run()

        assert ("gopay", intent_id) not in platform.sim.charges, "aucune bascule après ambiguïté"
        body = await _intent(client, sk, intent_id)
        assert (body["status"], body["operator"]) == ("succeeded", "ndiaga")

        async with sessionmaker() as session:
            message = await session.scalar(
                select(OutboxMessage).where(
                    OutboxMessage.aggregate_id == intent_id,
                    OutboxMessage.topic == OPERATOR_CHARGE_TOPIC,
                )
            )
        assert message is not None
        assert message.payload["pinned_operator"] == "ndiaga"

        # Reprise du message : l'intent est réglé, rien n'est renvoyé à personne.
        async with sessionmaker.begin() as session:
            await session.execute(
                text("UPDATE outbox_messages SET available_at = now() WHERE id = :id"),
                {"id": message.id},
            )
        platform.transport.calls.clear()
        await platform.run()
        assert sum(platform.transport.calls.values()) == 0
        async with sessionmaker() as session:
            published = await session.scalar(
                select(OutboxMessage.published_at).where(OutboxMessage.id == message.id)
            )
        assert published is not None


# ----------------------------------------------------------------------- XG-07


async def test_xg07_callback_after_expiry_is_recorded_without_state_change(
    client: httpx.AsyncClient,
    sk: dict[str, str],
    platform: Platform,
    onboarded_merchant: dict[str, Any],
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    await platform.chaos("gopay", latency_ms=[300, 300])
    intent_id = await _confirmed(client, sk, "78")
    await platform.dispatcher.run_batch()  # charge acceptée, callback dans 300 ms

    async with sessionmaker.begin() as session:
        await session.execute(
            text("UPDATE payment_intents SET expires_at = now() WHERE id = :id"), {"id": intent_id}
        )
    async with sessionmaker.begin() as session:
        assert intent_id in await expire_due_intents(session)
    await platform.sim.drain()  # le callback arrive après l'expiration

    assert (await _intent(client, sk, intent_id))["status"] == "expired"
    async with sessionmaker() as session:
        alert = await session.scalar(
            select(ReconciliationAlert.kind).where(ReconciliationAlert.intent_id == intent_id)
        )
        recorded = await session.scalar(
            select(OperatorCallback.disposition).where(OperatorCallback.intent_id == intent_id)
        )
        balances = await merchant_balances(session, onboarded_merchant["id"], livemode=False)
    assert alert == "late_callback"
    assert recorded == "late_callback"
    assert balances.pending == 0


# -------------------------------------------------------------- callbacks directs

SECRET_GOPAY = "dev-only-gopay-callback-secret-0001"


async def _post_gopay(
    client: httpx.AsyncClient, request: formats.CallbackRequest
) -> httpx.Response:
    return await client.post(
        "/v1/operators/gopay/callback", content=request.body, headers=request.headers
    )


async def test_callback_with_invalid_signature_is_401(client: httpx.AsyncClient) -> None:
    request = formats.gopay(
        "forged-secret-" + "x" * 30,
        reference="pi_X",
        operator_reference="GP-F",
        amount=100,
        ok=True,
    )
    response = await _post_gopay(client, request)
    assert response.status_code == 401
    assert response.json()["code"] == "invalid_signature"


async def test_callback_for_unknown_intent_is_acknowledged_and_alerted(
    client: httpx.AsyncClient,
) -> None:
    reference = f"GP-{uuid.uuid4().hex[:10]}"
    request = formats.gopay(
        SECRET_GOPAY, reference="pi_UNKNOWN", operator_reference=reference, amount=100, ok=True
    )
    response = await _post_gopay(client, request)
    assert response.status_code == 200
    assert response.json()["disposition"] == "unknown_intent"


async def test_callback_with_wrong_amount_does_not_succeed_the_intent(
    client: httpx.AsyncClient, sk: dict[str, str], platform: Platform
) -> None:
    await platform.chaos("gopay", latency_ms=[60_000, 60_000])  # le vrai callback n'arrivera pas
    intent_id = await _confirmed(client, sk, "78")
    await platform.dispatcher.run_batch()
    request = formats.gopay(
        SECRET_GOPAY, reference=intent_id, operator_reference="GP-WRONG", amount=1, ok=True
    )
    response = await _post_gopay(client, request)
    assert response.json()["disposition"] == "amount_mismatch"
    assert (await _intent(client, sk, intent_id))["status"] == "processing"
    for task in list(platform.sim._tasks):
        task.cancel()


async def test_metrics_expose_breakers_and_intents_by_status(client: httpx.AsyncClient) -> None:
    text_metrics = (await client.get("/metrics")).text
    assert 'xaalis_circuit_breaker_state{operator="gopay"}' in text_metrics
    assert "xaalis_payment_intents{" in text_metrics
    assert 'xaalis_outbox_pending_messages{family="webhooks"}' in text_metrics
