"""Diffuseur des changements d'état : PostgreSQL ``LISTEN`` → abonnés SSE de ce processus.

Chaque processus d'API ouvre **une** connexion dédiée qui écoute le canal
``xaalis_payment_events`` et redistribue les notifications aux files des flux ouverts
pour l'intent concerné. ``NOTIFY`` étant transactionnel, un abonné ne voit jamais un
changement annulé. En cas de coupure, la connexion est rétablie avec un délai croissant ;
les événements manqués entre-temps sont rattrapés par les flux depuis ``payment_events``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections import defaultdict
from dataclasses import dataclass
from typing import Final

import asyncpg

from xaalis.observability.logging import get_logger
from xaalis.payments.service import PAYMENT_EVENTS_CHANNEL

_log = get_logger(__name__)
_QUEUE_SIZE: Final = 100


@dataclass(frozen=True, slots=True)
class Notification:
    intent_id: str
    event_id: int
    status: str


class EventBroker:
    def __init__(self, database_url: str) -> None:
        # asyncpg ne comprend pas le préfixe de dialecte SQLAlchemy.
        self._dsn = database_url.replace("postgresql+asyncpg://", "postgresql://", 1)
        self._subscribers: defaultdict[str, set[asyncio.Queue[Notification]]] = defaultdict(set)
        self._task: asyncio.Task[None] | None = None
        self._connected = asyncio.Event()

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="sse-broker")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._connected.wait(), timeout=5)

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    def subscribe(self, intent_id: str) -> asyncio.Queue[Notification]:
        queue: asyncio.Queue[Notification] = asyncio.Queue(maxsize=_QUEUE_SIZE)
        self._subscribers[intent_id].add(queue)
        return queue

    def unsubscribe(self, intent_id: str, queue: asyncio.Queue[Notification]) -> None:
        queues = self._subscribers.get(intent_id)
        if queues is not None:
            queues.discard(queue)
            if not queues:
                del self._subscribers[intent_id]

    def _dispatch(self, _conn: object, _pid: int, _channel: str, payload: object) -> None:
        try:
            data = json.loads(str(payload))
            notification = Notification(data["intent_id"], int(data["event_id"]), data["status"])
        except (ValueError, KeyError, TypeError):
            _log.warning("sse_bad_notification")
            return
        for queue in list(self._subscribers.get(notification.intent_id, ())):
            # Un abonné trop lent perd des notifications : il rattrape depuis la base.
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(notification)

    async def _run(self) -> None:
        delay = 1
        while True:
            try:
                connection = await asyncpg.connect(self._dsn)
                await connection.add_listener(PAYMENT_EVENTS_CHANNEL, self._dispatch)
                self._connected.set()
                delay = 1
                _log.info("sse_broker_listening")
                closed = asyncio.Event()
                connection.add_termination_listener(lambda _, event=closed: event.set())
                try:
                    await closed.wait()
                finally:
                    self._connected.clear()
                    with contextlib.suppress(Exception):
                        await connection.close()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — on se reconnecte quoi qu'il arrive
                _log.warning("sse_broker_disconnected", error=type(exc).__name__, retry_in=delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30)
