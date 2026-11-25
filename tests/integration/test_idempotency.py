"""Idempotence (lot 2) : rejeu à l'octet près, 422 si corps différent, concurrence."""

import asyncio
import json
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.integration.conftest import bearer, idem, intent_payload
from xaalis.payments.models import PaymentIntent

URL = "/v1/payment-intents"


@pytest.fixture
def sk(onboarded_merchant: dict[str, Any]) -> dict[str, str]:
    return bearer(onboarded_merchant["secret_key"])


async def test_replay_returns_the_original_response_byte_for_byte(
    client: httpx.AsyncClient, sk: dict[str, str]
) -> None:
    payload, headers = intent_payload(), {**sk, **idem()}
    first = await client.post(URL, json=payload, headers=headers)
    replay = await client.post(URL, json=payload, headers=headers)

    assert first.status_code == replay.status_code == 201
    assert replay.content == first.content
    assert replay.headers["Idempotent-Replayed"] == "true"
    assert "Idempotent-Replayed" not in first.headers


async def test_replay_with_reformatted_but_identical_json_is_still_a_replay(
    client: httpx.AsyncClient, sk: dict[str, str]
) -> None:
    payload, key = intent_payload(), idem()
    first = await client.post(URL, json=payload, headers={**sk, **key})
    reformatted = json.dumps(dict(reversed(list(payload.items()))), indent=2).encode()
    replay = await client.post(
        URL, content=reformatted, headers={**sk, **key, "Content-Type": "application/json"}
    )
    assert replay.content == first.content
    assert replay.headers["Idempotent-Replayed"] == "true"


async def test_same_key_with_a_different_body_returns_422(
    client: httpx.AsyncClient, sk: dict[str, str]
) -> None:
    key = idem()
    await client.post(URL, json=intent_payload(amount=1_000), headers={**sk, **key})
    response = await client.post(URL, json=intent_payload(amount=2_000), headers={**sk, **key})
    assert response.status_code == 422
    assert response.json()["code"] == "idempotency_key_reused"


async def test_same_key_on_another_endpoint_returns_422(
    client: httpx.AsyncClient, sk: dict[str, str]
) -> None:
    key = idem()
    created = (await client.post(URL, json=intent_payload(), headers={**sk, **key})).json()
    response = await client.post(f"{URL}/{created['id']}/confirm", headers={**sk, **key})
    assert response.status_code == 422


async def test_keys_are_scoped_per_merchant(
    client: httpx.AsyncClient, admin_headers: dict[str, str], sk: dict[str, str]
) -> None:
    other = (
        await client.post("/v1/admin/merchants", json={"name": "Autre"}, headers=admin_headers)
    ).json()
    key, payload = idem("shared-key"), intent_payload()
    mine = await client.post(URL, json=payload, headers={**sk, **key})
    theirs = await client.post(URL, json=payload, headers={**bearer(other["secret_key"]), **key})
    assert mine.json()["id"] != theirs.json()["id"]
    assert "Idempotent-Replayed" not in theirs.headers


async def test_twenty_concurrent_requests_with_the_same_key_create_one_intent(
    client: httpx.AsyncClient,
    sk: dict[str, str],
    onboarded_merchant: dict[str, Any],
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Exigence de l'énoncé : 20 requêtes simultanées, même clé → un seul intent."""
    payload, headers = intent_payload(), {**sk, **idem()}

    responses = await asyncio.gather(
        *(client.post(URL, json=payload, headers=headers) for _ in range(20))
    )

    assert {r.status_code for r in responses} == {201}
    assert len({r.content for r in responses}) == 1, "toutes les réponses sont identiques"
    assert sum("Idempotent-Replayed" not in r.headers for r in responses) == 1
    async with sessionmaker() as session:
        count = await session.scalar(
            select(func.count()).where(PaymentIntent.merchant_id == onboarded_merchant["id"])
        )
    assert count == 1


async def test_key_older_than_24_hours_can_be_reused(
    client: httpx.AsyncClient,
    sk: dict[str, str],
    onboarded_merchant: dict[str, Any],
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    key = idem()
    first = await client.post(URL, json=intent_payload(), headers={**sk, **key})
    async with sessionmaker.begin() as session:
        await session.execute(
            text(
                "UPDATE idempotency_keys SET expires_at = now() - interval '1 second' "
                "WHERE merchant_id = :m"
            ),
            {"m": onboarded_merchant["id"]},
        )
    second = await client.post(URL, json=intent_payload(amount=5_000), headers={**sk, **key})
    assert second.status_code == 201
    assert second.json()["id"] != first.json()["id"]


async def test_a_failed_request_does_not_burn_the_key(
    client: httpx.AsyncClient, sk: dict[str, str]
) -> None:
    """Un 409 annule la transaction, réservation de la clé comprise : rien n'est figé."""
    created = (await client.post(URL, json=intent_payload(), headers={**sk, **idem()})).json()
    await client.post(f"{URL}/{created['id']}/cancel", headers=sk)
    key = idem()
    first = await client.post(f"{URL}/{created['id']}/confirm", headers={**sk, **key})
    again = await client.post(f"{URL}/{created['id']}/confirm", headers={**sk, **key})
    assert first.status_code == again.status_code == 409
    assert "Idempotent-Replayed" not in again.headers
