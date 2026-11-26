"""Lot 7 : flux SSE des changements d'état (GET /v1/payment-intents/{id}/stream, clé pk_)."""

import asyncio
import json
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.integration.conftest import bearer, idem, intent_payload
from xaalis.payments.service import Scope, cancel_intent, create_intent
from xaalis.realtime.broker import EventBroker
from xaalis.realtime.stream import parse_last_event_id, stream_intent

URL = "/v1/payment-intents"


def _events(body: str) -> list[dict[str, Any]]:
    events = []
    for block in body.strip().split("\n\n"):
        fields = dict(
            line.split(": ", 1)
            for line in block.splitlines()
            if ": " in line and not line.startswith(":")
        )
        if "data" in fields:
            events.append(
                {"id": int(fields["id"]), "event": fields["event"], **json.loads(fields["data"])}
            )
    return events


@pytest.fixture
def keys(onboarded_merchant: dict[str, Any]) -> tuple[dict[str, str], dict[str, str]]:
    return bearer(onboarded_merchant["secret_key"]), bearer(onboarded_merchant["publishable_key"])


async def _create(client: httpx.AsyncClient, sk: dict[str, str]) -> str:
    response = await client.post(URL, json=intent_payload(), headers={**sk, **idem()})
    intent_id: str = response.json()["id"]
    return intent_id


async def test_stream_pushes_history_then_live_changes_and_closes_when_terminal(
    client: httpx.AsyncClient, keys: tuple[dict[str, str], dict[str, str]]
) -> None:
    sk, pk = keys
    intent_id = await _create(client, sk)
    stream = asyncio.create_task(client.get(f"{URL}/{intent_id}/stream", headers=pk))
    await asyncio.sleep(0.3)  # le flux est ouvert et abonné
    assert not stream.done()

    await client.post(f"{URL}/{intent_id}/cancel", headers=sk)
    response = await asyncio.wait_for(stream, timeout=5)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-cache"
    events = _events(response.text)
    assert [(e["previous_status"], e["status"]) for e in events] == [
        (None, "created"),
        ("created", "canceled"),
    ]
    assert all(e["event"] == "status_changed" and e["payment_intent"] == intent_id for e in events)
    assert events[0]["id"] < events[1]["id"]
    assert response.text.startswith("retry: ")


async def test_reconnection_with_last_event_id_only_sends_what_was_missed(
    client: httpx.AsyncClient, keys: tuple[dict[str, str], dict[str, str]]
) -> None:
    sk, pk = keys
    intent_id = await _create(client, sk)
    await client.post(f"{URL}/{intent_id}/cancel", headers=sk)
    full = _events((await client.get(f"{URL}/{intent_id}/stream", headers=pk)).text)

    resumed = await client.get(
        f"{URL}/{intent_id}/stream", headers={**pk, "Last-Event-ID": str(full[0]["id"])}
    )
    assert [e["status"] for e in _events(resumed.text)] == ["canceled"]


async def test_stream_of_another_merchants_intent_is_404(
    client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    keys: tuple[dict[str, str], dict[str, str]],
) -> None:
    _, pk = keys
    other = (
        await client.post("/v1/admin/merchants", json={"name": "Autre"}, headers=admin_headers)
    ).json()
    foreign = await _create(client, bearer(other["secret_key"]))
    response = await client.get(f"{URL}/{foreign}/stream", headers=pk)
    assert response.status_code == 404


async def test_stream_requires_a_key(client: httpx.AsyncClient) -> None:
    response = await client.get(f"{URL}/pi_01JD8Q3W7Z4K2N6R5T9V0XBCYA/stream")
    assert response.status_code == 401


async def test_notify_is_transactional_rolled_back_changes_are_never_pushed(
    app: FastAPI,
    sessionmaker: async_sessionmaker[AsyncSession],
    onboarded_merchant: dict[str, Any],
) -> None:
    broker: EventBroker = app.state.broker
    scope = Scope(onboarded_merchant["id"], False)
    async with sessionmaker.begin() as session:
        intent = await create_intent(
            session, scope, amount=1_000, currency="XOF", payer_phone="+221781234567", actor="t"
        )
    queue = broker.subscribe(intent.id)
    try:
        session = sessionmaker()
        async with session:
            await session.begin()
            await cancel_intent(session, scope, intent.id, actor="t")
            await session.rollback()
        await asyncio.sleep(0.3)
        assert queue.empty(), "aucune notification pour une transaction annulée"

        async with sessionmaker.begin() as session:
            await cancel_intent(session, scope, intent.id, actor="t")
        notification = await asyncio.wait_for(queue.get(), timeout=3)
        assert (notification.intent_id, notification.status) == (intent.id, "canceled")
    finally:
        broker.unsubscribe(intent.id, queue)


async def test_open_stream_sends_heartbeats_and_respects_its_maximum_lifetime(
    app: FastAPI,
    sessionmaker: async_sessionmaker[AsyncSession],
    onboarded_merchant: dict[str, Any],
) -> None:
    async with sessionmaker.begin() as session:
        intent = await create_intent(
            session,
            Scope(onboarded_merchant["id"], False),
            amount=1_000,
            currency="XOF",
            payer_phone="+221781234568",
            actor="t",
        )

    async def connected() -> bool:
        return False

    chunks = [
        chunk
        async for chunk in stream_intent(
            intent_id=intent.id,
            sessionmaker=sessionmaker,
            broker=app.state.broker,
            last_event_id=0,
            is_disconnected=connected,
            heartbeat_seconds=1,
            max_seconds=2,
        )
    ]
    assert any(chunk.startswith(": keep-alive") for chunk in chunks)
    assert sum("status_changed" in chunk for chunk in chunks) == 1  # « created » seulement


async def test_client_disconnect_ends_the_stream(
    app: FastAPI, sessionmaker: async_sessionmaker[AsyncSession], onboarded_merchant: dict[str, Any]
) -> None:
    async with sessionmaker.begin() as session:
        intent = await create_intent(
            session,
            Scope(onboarded_merchant["id"], False),
            amount=1_000,
            currency="XOF",
            payer_phone="+221781234569",
            actor="t",
        )

    async def gone() -> bool:
        return True

    chunks = [
        chunk
        async for chunk in stream_intent(
            intent_id=intent.id,
            sessionmaker=sessionmaker,
            broker=app.state.broker,
            last_event_id=0,
            is_disconnected=gone,
            heartbeat_seconds=1,
            max_seconds=30,
        )
    ]
    assert not any(chunk.startswith(": keep-alive") for chunk in chunks)


@pytest.mark.parametrize(
    ("raw", "expected"), [(None, 0), ("", 0), ("42", 42), ("x", 0), ("9" * 30, 0)]
)
def test_last_event_id_parsing(raw: str | None, expected: int) -> None:
    assert parse_last_event_id(raw) == expected


def test_malformed_notification_is_ignored() -> None:
    broker = EventBroker("postgresql+asyncpg://unused")
    queue = broker.subscribe("pi_X")
    broker._dispatch(None, 0, "c", "not json")
    broker._dispatch(None, 0, "c", json.dumps({"intent_id": "pi_X"}))
    assert queue.empty()
