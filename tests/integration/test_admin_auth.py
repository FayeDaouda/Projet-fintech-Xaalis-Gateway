"""Connexion administrateur (Argon2id) et JWT."""

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from xaalis.admin.service import create_admin
from xaalis.core.ids import new_id

LOGIN_URL = "/v1/admin/auth/token"


async def _admin(sessionmaker: async_sessionmaker[AsyncSession], password: str) -> str:
    email = f"{new_id('adm').lower()}@xaalis.example"
    async with sessionmaker.begin() as session:
        await create_admin(session, email, password)
    return email


async def test_admin_can_log_in_and_use_the_token(
    client: httpx.AsyncClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    email = await _admin(sessionmaker, "correct horse battery staple")
    response = await client.post(
        LOGIN_URL, json={"email": email.upper(), "password": "correct horse battery staple"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "bearer"
    assert body["expires_in"] > 0

    health = await client.get(
        "/v1/admin/ledger/health", headers={"Authorization": f"Bearer {body['access_token']}"}
    )
    assert health.status_code == 200


async def test_wrong_password_and_unknown_email_are_indistinguishable(
    client: httpx.AsyncClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    email = await _admin(sessionmaker, "the-real-password")
    wrong = await client.post(LOGIN_URL, json={"email": email, "password": "not-it"})
    unknown = await client.post(
        LOGIN_URL, json={"email": "nobody@xaalis.example", "password": "not-it"}
    )
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json()["code"] == unknown.json()["code"] == "invalid_credentials"
    assert wrong.json()["detail"] == unknown.json()["detail"]


async def test_password_hash_is_argon2id(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    async with sessionmaker.begin() as session:
        admin = await create_admin(session, f"{new_id('adm').lower()}@x.example", "pw-123456")
    assert admin.password_hash.startswith("$argon2id$")
    assert "pw-123456" not in admin.password_hash
