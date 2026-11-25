"""POST/GET /v1/admin/merchants : création, clés affichées une fois, pagination."""

from typing import Any

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.integration.conftest import bearer
from xaalis.merchants.keys import parse_key
from xaalis.merchants.models import ApiKey

MERCHANTS_URL = "/v1/admin/merchants"


async def test_creation_returns_test_and_live_keys_once(onboarded_merchant: dict[str, Any]) -> None:
    body = onboarded_merchant
    assert body["object"] == "merchant"
    assert body["id"].startswith("m_")
    assert body["publishable_key"].startswith("pk_test_")
    assert body["secret_key"].startswith("sk_test_")
    assert body["live_keys"]["publishable_key"].startswith("pk_live_")
    assert body["live_keys"]["secret_key"].startswith("sk_live_")


async def test_only_fingerprints_are_stored(
    onboarded_merchant: dict[str, Any], sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    secret = onboarded_merchant["secret_key"]
    async with sessionmaker() as session:
        rows = (
            await session.scalars(
                select(ApiKey).where(ApiKey.merchant_id == onboarded_merchant["id"])
            )
        ).all()
    assert len(rows) == 4
    for row in rows:
        assert len(row.fingerprint) == 32
        assert secret not in row.redacted
        assert secret.encode() not in row.fingerprint


async def test_keys_are_never_returned_again(
    client: httpx.AsyncClient, admin_headers: dict[str, str], onboarded_merchant: dict[str, Any]
) -> None:
    response = await client.get(
        f"{MERCHANTS_URL}/{onboarded_merchant['id']}", headers=admin_headers
    )
    assert response.status_code == 200
    assert set(response.json()) == {"id", "object", "name", "created_at"}


async def test_created_keys_authenticate(
    client: httpx.AsyncClient, onboarded_merchant: dict[str, Any]
) -> None:
    for key in (onboarded_merchant["secret_key"], onboarded_merchant["live_keys"]["secret_key"]):
        response = await client.post("/v1/api-keys/rotate", headers=bearer(key))
        assert response.status_code == 201
        parsed = parse_key(response.json()["secret_key"])
        assert parsed is not None
        assert parsed.livemode is response.json()["livemode"]


async def test_merchant_creation_requires_admin(client: httpx.AsyncClient) -> None:
    response = await client.post(MERCHANTS_URL, json={"name": "Intrus"})
    assert response.status_code == 401


async def test_merchant_key_cannot_call_admin_routes(
    client: httpx.AsyncClient, onboarded_merchant: dict[str, Any]
) -> None:
    response = await client.get(MERCHANTS_URL, headers=bearer(onboarded_merchant["secret_key"]))
    assert response.status_code == 401
    assert response.json()["code"] == "invalid_token"


@pytest.mark.parametrize(
    "payload",
    [{}, {"name": ""}, {"name": "x" * 201}, {"name": "ok", "is_admin": True}],
    ids=["missing", "empty", "too-long", "unknown-field"],
)
async def test_invalid_creation_payload_is_rejected(
    client: httpx.AsyncClient, admin_headers: dict[str, str], payload: dict[str, object]
) -> None:
    response = await client.post(MERCHANTS_URL, json=payload, headers=admin_headers)
    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"


async def test_unknown_merchant_is_404(
    client: httpx.AsyncClient, admin_headers: dict[str, str]
) -> None:
    response = await client.get(
        f"{MERCHANTS_URL}/m_01JD8Q3W7Z4K2N6R5T9V0XBCYA", headers=admin_headers
    )
    assert response.status_code == 404
    assert response.json()["code"] == "not_found"


async def test_cursor_pagination_walks_every_merchant_exactly_once(
    client: httpx.AsyncClient, admin_headers: dict[str, str]
) -> None:
    created = []
    for i in range(5):
        response = await client.post(
            MERCHANTS_URL, json={"name": f"Pagination {i}"}, headers=admin_headers
        )
        created.append(response.json()["id"])

    seen: list[str] = []
    cursor: str | None = None
    while True:
        params: dict[str, str | int] = {"limit": 2}
        if cursor:
            params["starting_after"] = cursor
        page = (await client.get(MERCHANTS_URL, params=params, headers=admin_headers)).json()
        assert page["object"] == "list"
        assert len(page["data"]) <= 2
        seen.extend(item["id"] for item in page["data"])
        if not page["has_more"]:
            assert page["next_cursor"] is None
            break
        cursor = page["next_cursor"]

    assert len(seen) == len(set(seen)), "aucun doublon entre les pages"
    assert seen == sorted(seen, reverse=True), "du plus récent au plus ancien"
    assert set(created) <= set(seen)


@pytest.mark.parametrize("limit", [0, 101])
async def test_page_size_is_capped_at_100(
    client: httpx.AsyncClient, admin_headers: dict[str, str], limit: int
) -> None:
    response = await client.get(MERCHANTS_URL, params={"limit": limit}, headers=admin_headers)
    assert response.status_code == 422
