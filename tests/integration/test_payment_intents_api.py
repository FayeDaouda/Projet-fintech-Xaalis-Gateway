"""Intents de paiement de bout en bout : contrat, règles XG-01/02/03, cycle de vie."""

from datetime import timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.integration.conftest import bearer, idem, intent_payload, random_phone
from xaalis.outbox.models import OutboxMessage
from xaalis.payments.models import PaymentEvent

URL = "/v1/payment-intents"


@pytest.fixture
def sk(onboarded_merchant: dict[str, Any]) -> dict[str, str]:
    return bearer(onboarded_merchant["secret_key"])


async def _create(client: httpx.AsyncClient, sk: dict[str, str], **overrides: object) -> Any:
    response = await client.post(URL, json=intent_payload(**overrides), headers={**sk, **idem()})
    assert response.status_code == 201, response.text
    return response.json()


async def test_created_intent_matches_the_contract(
    client: httpx.AsyncClient, sk: dict[str, str]
) -> None:
    body = await _create(client, sk, payer_phone="+221771234512")
    assert body["id"].startswith("pi_")
    assert body["object"] == "payment_intent"
    assert body["livemode"] is False
    assert (body["amount"], body["fee"], body["net"]) == (12_345, 223, 12_122)  # XG-04
    assert body["currency"] == "XOF"
    assert body["status"] == "created"
    assert body["payer_phone"] == "+221 77 *** ** 12"  # XG-03
    assert body["operator"] is None
    assert body["amount_refunded"] == 0
    assert body["metadata"] == {"order_id": "CMD-2026-0042"}


async def test_xg07_intent_expires_fifteen_minutes_after_creation(
    client: httpx.AsyncClient, sk: dict[str, str]
) -> None:
    from datetime import datetime

    body = await _create(client, sk)
    created = datetime.fromisoformat(body["created_at"])
    expires = datetime.fromisoformat(body["expires_at"])
    assert expires - created == timedelta(minutes=15)


@pytest.mark.parametrize(
    ("override", "field"),
    [
        ({"amount": 99}, "amount"),
        ({"amount": 2_000_001}, "amount"),
        ({"amount": 1000.0}, "amount"),
        ({"amount": "1000"}, "amount"),
        ({"amount": True}, "amount"),
    ],
    ids=["below-min", "above-max", "float", "string", "bool"],
)
async def test_xg01_amount_must_be_an_integer_within_bounds(
    client: httpx.AsyncClient, sk: dict[str, str], override: dict[str, object], field: str
) -> None:
    response = await client.post(URL, json=intent_payload(**override), headers={**sk, **idem()})
    assert response.status_code == 422
    assert response.json()["errors"][0]["loc"][-1] == field


@pytest.mark.parametrize("currency", ["EUR", "USD", "xof", "XAF"])
async def test_xg02_only_xof_is_accepted(
    client: httpx.AsyncClient, sk: dict[str, str], currency: str
) -> None:
    response = await client.post(
        URL, json=intent_payload(currency=currency), headers={**sk, **idem()}
    )
    assert response.status_code == 422


@pytest.mark.parametrize("phone", ["771234512", "+221671234512", "+33612345678"])
async def test_xg03_invalid_phone_is_rejected_and_never_echoed(
    client: httpx.AsyncClient, sk: dict[str, str], phone: str
) -> None:
    response = await client.post(
        URL, json=intent_payload(payer_phone=phone), headers={**sk, **idem()}
    )
    assert response.status_code == 422
    assert phone not in response.text


async def test_unknown_fields_are_rejected(client: httpx.AsyncClient, sk: dict[str, str]) -> None:
    response = await client.post(
        URL, json=intent_payload(status="succeeded"), headers={**sk, **idem()}
    )
    assert response.status_code == 422


async def test_idempotency_key_is_mandatory_on_creation(
    client: httpx.AsyncClient, sk: dict[str, str]
) -> None:
    response = await client.post(URL, json=intent_payload(), headers=sk)
    assert response.status_code == 400
    assert response.json()["code"] == "idempotency_key_required"


async def test_publishable_key_can_read_but_not_create(
    client: httpx.AsyncClient, sk: dict[str, str], onboarded_merchant: dict[str, Any]
) -> None:
    pk = bearer(onboarded_merchant["publishable_key"])
    created = await _create(client, sk)
    assert (await client.get(f"{URL}/{created['id']}", headers=pk)).status_code == 200
    denied = await client.post(URL, json=intent_payload(), headers={**pk, **idem()})
    assert denied.status_code == 403


async def test_confirm_routes_to_cheapest_operator_and_returns_202(
    client: httpx.AsyncClient, sk: dict[str, str]
) -> None:
    created = await _create(client, sk, payer_phone=random_phone("77"))
    response = await client.post(f"{URL}/{created['id']}/confirm", headers={**sk, **idem()})
    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "processing"
    assert body["operator"] == "ndiaga"  # XG-06 : 0,8 % < 0,9 %


async def test_cancel_then_invalid_transitions_return_409_problem(
    client: httpx.AsyncClient, sk: dict[str, str]
) -> None:
    created = await _create(client, sk)
    canceled = await client.post(f"{URL}/{created['id']}/cancel", headers=sk)
    assert canceled.status_code == 200
    assert canceled.json()["status"] == "canceled"

    for action in ("cancel", "confirm"):
        response = await client.post(f"{URL}/{created['id']}/{action}", headers={**sk, **idem()})
        assert response.status_code == 409
        problem = response.json()
        assert response.headers["content-type"] == "application/problem+json"
        assert problem["code"] == "invalid_transition"
        assert problem["type"].endswith("/invalid-transition")


async def test_processing_intent_cannot_be_canceled(
    client: httpx.AsyncClient, sk: dict[str, str]
) -> None:
    created = await _create(client, sk)
    await client.post(f"{URL}/{created['id']}/confirm", headers={**sk, **idem()})
    response = await client.post(f"{URL}/{created['id']}/cancel", headers=sk)
    assert response.status_code == 409


async def test_every_transition_is_recorded_with_actor_and_published_to_outbox(
    client: httpx.AsyncClient, sk: dict[str, str], sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    created = await _create(client, sk)
    await client.post(f"{URL}/{created['id']}/confirm", headers={**sk, **idem()})

    async with sessionmaker() as session:
        events = (
            await session.scalars(
                select(PaymentEvent)
                .where(PaymentEvent.intent_id == created["id"])
                .order_by(PaymentEvent.id)
            )
        ).all()
        topics = (
            await session.scalars(
                select(OutboxMessage.topic)
                .where(OutboxMessage.aggregate_id == created["id"])
                .order_by(OutboxMessage.id)
            )
        ).all()
    assert [(e.from_status, e.to_status, e.cause) for e in events] == [
        (None, "created", "create"),
        ("created", "processing", "confirm"),
    ]
    assert all(e.actor.startswith("merchant:key_") for e in events)
    assert list(topics) == ["payment.created", "payment.processing", "internal.operator.charge"]


async def test_outbox_payload_never_contains_the_full_phone_number(
    client: httpx.AsyncClient, sk: dict[str, str], sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    phone = random_phone()
    created = await _create(client, sk, payer_phone=phone)
    async with sessionmaker() as session:
        payloads = (
            await session.scalars(
                select(OutboxMessage.payload).where(OutboxMessage.aggregate_id == created["id"])
            )
        ).all()
    assert payloads
    assert all(phone not in str(payload) for payload in payloads)


async def test_list_filters_by_status_and_paginates(
    client: httpx.AsyncClient, sk: dict[str, str]
) -> None:
    ids = [(await _create(client, sk))["id"] for _ in range(3)]
    await client.post(f"{URL}/{ids[0]}/cancel", headers=sk)

    canceled = (await client.get(URL, params={"status": "canceled"}, headers=sk)).json()
    assert [item["id"] for item in canceled["data"]] == [ids[0]]

    first = (await client.get(URL, params={"limit": 2}, headers=sk)).json()
    assert first["has_more"] is True
    second = (
        await client.get(
            URL, params={"limit": 2, "starting_after": first["next_cursor"]}, headers=sk
        )
    ).json()
    assert [i["id"] for i in first["data"] + second["data"]] == sorted(ids, reverse=True)


async def test_unknown_status_filter_is_rejected(
    client: httpx.AsyncClient, sk: dict[str, str]
) -> None:
    response = await client.get(URL, params={"status": "paid"}, headers=sk)
    assert response.status_code == 422


# ------------------------------------------------------- XG-12 et OWASP API1 (BOLA)


async def test_xg12_test_key_never_sees_live_intent_and_vice_versa(
    client: httpx.AsyncClient, onboarded_merchant: dict[str, Any]
) -> None:
    test_sk = bearer(onboarded_merchant["secret_key"])
    live_sk = bearer(onboarded_merchant["live_keys"]["secret_key"])
    live_intent = await _create(client, live_sk)
    assert live_intent["livemode"] is True
    test_intent = await _create(client, test_sk)

    for headers, foreign in ((test_sk, live_intent), (live_sk, test_intent)):
        response = await client.get(f"{URL}/{foreign['id']}", headers=headers)
        assert response.status_code == 404  # 404 et non 403 : rien n'est révélé
        listing = (await client.get(URL, headers=headers)).json()
        assert foreign["id"] not in {item["id"] for item in listing["data"]}


async def test_bola_merchant_cannot_read_or_act_on_another_merchants_intent(
    client: httpx.AsyncClient, admin_headers: dict[str, str], sk: dict[str, str]
) -> None:
    other = (
        await client.post("/v1/admin/merchants", json={"name": "Concurrent"}, headers=admin_headers)
    ).json()
    foreign = await _create(client, bearer(other["secret_key"]))

    assert (await client.get(f"{URL}/{foreign['id']}", headers=sk)).status_code == 404
    for action in ("confirm", "cancel"):
        response = await client.post(f"{URL}/{foreign['id']}/{action}", headers={**sk, **idem()})
        assert response.status_code == 404
