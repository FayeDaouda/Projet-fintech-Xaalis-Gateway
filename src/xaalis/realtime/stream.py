"""Flux SSE des changements d'état d'un intent (lot 7).

Protocole : ``retry`` puis un événement par changement d'état ::

    id: 1234
    event: status_changed
    data: {"payment_intent": "pi_…", "previous_status": "processing", "status": "succeeded", …}

- Sans ``Last-Event-ID`` : tout l'historique de l'intent, puis le direct.
- Avec ``Last-Event-ID`` (reconnexion du navigateur) : uniquement ce qui a été manqué.
- On s'abonne **avant** de lire l'historique, puis on déduplique par ``id`` : aucun
  changement ne peut tomber entre la lecture et l'abonnement.
- Le flux se ferme dans un état terminal, ou au bout de 20 minutes ; un commentaire de
  maintien de connexion part toutes les 15 secondes.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from xaalis.payments.models import PaymentEvent
from xaalis.payments.state_machine import TERMINAL_STATUSES, IntentStatus
from xaalis.realtime.broker import EventBroker

HEARTBEAT_SECONDS: Final = 15
MAX_STREAM_SECONDS: Final = 20 * 60
RETRY_MS: Final = 3_000


def _format(event: PaymentEvent) -> str:
    data = {
        "payment_intent": event.intent_id,
        "previous_status": event.from_status,
        "status": event.to_status,
        "cause": event.cause,
        "occurred_at": event.created_at.isoformat().replace("+00:00", "Z"),
    }
    return f"id: {event.id}\nevent: status_changed\ndata: {json.dumps(data)}\n\n"


async def _events_after(
    sessionmaker: async_sessionmaker[AsyncSession], intent_id: str, after: int
) -> list[PaymentEvent]:
    async with sessionmaker() as session:
        rows = await session.scalars(
            select(PaymentEvent)
            .where(PaymentEvent.intent_id == intent_id, PaymentEvent.id > after)
            .order_by(PaymentEvent.id)
        )
        # Seuls les changements d'état (une bascule d'opérateur n'en est pas un).
        return [e for e in rows if e.from_status != e.to_status]


def parse_last_event_id(raw: str | None) -> int:
    return int(raw) if raw and raw.isdigit() and len(raw) < 19 else 0


async def stream_intent(
    *,
    intent_id: str,
    sessionmaker: async_sessionmaker[AsyncSession],
    broker: EventBroker,
    last_event_id: int,
    is_disconnected: Callable[[], Awaitable[bool]],
    heartbeat_seconds: int = HEARTBEAT_SECONDS,
    max_seconds: int = MAX_STREAM_SECONDS,
) -> AsyncIterator[str]:
    queue = broker.subscribe(intent_id)
    deadline = time.monotonic() + max_seconds
    last = last_event_id
    try:
        yield f"retry: {RETRY_MS}\n\n"
        while True:
            for event in await _events_after(sessionmaker, intent_id, last):
                yield _format(event)
                last = event.id
                if IntentStatus(event.to_status) in TERMINAL_STATUSES:
                    return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            try:
                notification = await asyncio.wait_for(
                    queue.get(), timeout=min(heartbeat_seconds, remaining)
                )
            except TimeoutError:
                if await is_disconnected():
                    return
                yield ": keep-alive\n\n"
                continue
            if notification.event_id <= last:
                continue  # déjà envoyé lors du rattrapage
    finally:
        broker.unsubscribe(intent_id, queue)
