"""Authentification par clé d'API, rotation (24 h de grâce) et signature des requêtes."""

import json
import time
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.integration.conftest import bearer
from xaalis.api.auth import SignedRequestDep
from xaalis.core.signatures import sign

ROTATE_URL = "/v1/api-keys/rotate"


async def _expire_key_now(sessionmaker: async_sessionmaker[AsyncSession], key_id: str) -> None:
    """Simule l'écoulement des 24 h de grâce."""
    async with sessionmaker.begin() as session:
        await session.execute(
            text("UPDATE api_keys SET expires_at = now() - interval '1 second' WHERE id = :id"),
            {"id": key_id},
        )


async def test_missing_key_is_401(client: httpx.AsyncClient) -> None:
    response = await client.post(ROTATE_URL)
    assert response.status_code == 401
    assert response.json()["code"] == "unauthenticated"


@pytest.mark.parametrize(
    "key",
    ["garbage", "sk_test_" + "A" * 32, "sk_live_" + "b" * 32],
    ids=["malformed", "unknown", "unknown-live"],
)
async def test_invalid_key_is_401(client: httpx.AsyncClient, key: str) -> None:
    response = await client.post(ROTATE_URL, headers=bearer(key))
    assert response.status_code == 401
    assert response.json()["code"] == "invalid_api_key"


async def test_publishable_key_cannot_rotate(
    client: httpx.AsyncClient, onboarded_merchant: dict[str, Any]
) -> None:
    response = await client.post(ROTATE_URL, headers=bearer(onboarded_merchant["publishable_key"]))
    assert response.status_code == 403
    assert response.json()["code"] == "secret_key_required"


async def test_rotation_keeps_old_key_valid_for_24_hours(
    client: httpx.AsyncClient,
    onboarded_merchant: dict[str, Any],
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    old_key = onboarded_merchant["secret_key"]
    response = await client.post(ROTATE_URL, headers=bearer(old_key))
    assert response.status_code == 201
    body = response.json()
    new_key = body["secret_key"]
    assert new_key != old_key
    assert body["livemode"] is False

    async with sessionmaker() as session:
        grace = await session.scalar(
            text("SELECT expires_at - created_at FROM api_keys WHERE id = :id"),
            {"id": body["previous_key"]["id"]},
        )
        new_expiry = await session.scalar(
            text("SELECT expires_at FROM api_keys WHERE id = :id"), {"id": body["id"]}
        )
    assert grace is not None
    assert 24 * 3600 - 60 <= grace.total_seconds() <= 24 * 3600 + 60
    assert new_expiry is None

    # Pendant la période de grâce, les deux clés fonctionnent.
    assert (await client.post(ROTATE_URL, headers=bearer(new_key))).status_code == 201
    assert (await client.post(ROTATE_URL, headers=bearer(old_key))).status_code == 201

    # Après 24 h, l'ancienne est refusée, la nouvelle fonctionne toujours.
    await _expire_key_now(sessionmaker, body["previous_key"]["id"])
    assert (await client.post(ROTATE_URL, headers=bearer(old_key))).status_code == 401
    assert (await client.post(ROTATE_URL, headers=bearer(new_key))).status_code == 201


async def test_rotating_an_old_key_again_does_not_extend_its_life(
    client: httpx.AsyncClient,
    onboarded_merchant: dict[str, Any],
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    old_key = onboarded_merchant["secret_key"]
    first = (await client.post(ROTATE_URL, headers=bearer(old_key))).json()
    second = (await client.post(ROTATE_URL, headers=bearer(old_key))).json()
    assert second["previous_key"]["expires_at"] == first["previous_key"]["expires_at"]


async def test_revoked_key_is_refused(
    client: httpx.AsyncClient,
    onboarded_merchant: dict[str, Any],
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with sessionmaker.begin() as session:
        await session.execute(
            text(
                "UPDATE api_keys SET revoked_at = now() "
                "WHERE merchant_id = :m AND kind = 'secret' AND NOT livemode"
            ),
            {"m": onboarded_merchant["id"]},
        )
    response = await client.post(ROTATE_URL, headers=bearer(onboarded_merchant["secret_key"]))
    assert response.status_code == 401


# --------------------------------------------------------------- signature HMAC


@pytest.fixture(scope="module")
def signed_probe(app: FastAPI) -> str:
    path = "/__test/signed"

    @app.post(path, include_in_schema=False)
    async def signed_endpoint(principal: SignedRequestDep) -> dict[str, str]:
        return {"merchant_id": principal.merchant_id}

    return path


def _signed_headers(secret: str, body: bytes, *, timestamp: int | None = None) -> dict[str, str]:
    ts = timestamp if timestamp is not None else int(time.time())
    return {
        **bearer(secret),
        "Content-Type": "application/json",
        "X-Xaalis-Timestamp": str(ts),
        "X-Xaalis-Signature": sign(secret, ts, body),
    }


async def test_correctly_signed_request_is_accepted(
    client: httpx.AsyncClient, onboarded_merchant: dict[str, Any], signed_probe: str
) -> None:
    secret = onboarded_merchant["secret_key"]
    body = json.dumps({"amount": 1000}).encode()
    response = await client.post(signed_probe, content=body, headers=_signed_headers(secret, body))
    assert response.status_code == 200
    assert response.json() == {"merchant_id": onboarded_merchant["id"]}


async def test_unsigned_request_is_rejected(
    client: httpx.AsyncClient, onboarded_merchant: dict[str, Any], signed_probe: str
) -> None:
    response = await client.post(
        signed_probe, json={"amount": 1000}, headers=bearer(onboarded_merchant["secret_key"])
    )
    assert response.status_code == 401
    assert response.json()["code"] == "signature_required"


async def test_request_signed_more_than_300_seconds_ago_is_rejected(
    client: httpx.AsyncClient, onboarded_merchant: dict[str, Any], signed_probe: str
) -> None:
    secret = onboarded_merchant["secret_key"]
    body = b'{"amount":1000}'
    headers = _signed_headers(secret, body, timestamp=int(time.time()) - 301)
    response = await client.post(signed_probe, content=body, headers=headers)
    assert response.status_code == 401
    assert response.json()["code"] == "signature_expired"


async def test_tampered_body_is_rejected(
    client: httpx.AsyncClient, onboarded_merchant: dict[str, Any], signed_probe: str
) -> None:
    secret = onboarded_merchant["secret_key"]
    headers = _signed_headers(secret, b'{"amount":1000}')
    response = await client.post(signed_probe, content=b'{"amount":9000}', headers=headers)
    assert response.status_code == 401
    assert response.json()["code"] == "invalid_signature"
