"""Tâches périodiques du worker.

Chaque tâche est une boucle « exécuter un lot, puis attendre » qui s'arrête proprement
dès que l'événement ``stop`` est positionné. Une erreur sur un lot est journalisée et la
boucle continue : une panne transitoire de la base ne doit pas tuer le worker.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from xaalis.idempotency.service import purge_expired
from xaalis.observability.logging import get_logger
from xaalis.payments.service import expire_due_intents
from xaalis.worker.runner import Job

_log = get_logger(__name__)


def periodic(name: str, interval_seconds: int, step: Callable[[], Awaitable[int]]) -> Job:
    async def loop(stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                done = await step()
                if done:
                    _log.info("job_batch_done", job=name, count=done)
            except Exception:  # noqa: BLE001 — on journalise et on réessaie au tour suivant
                _log.exception("job_batch_failed", job=name)
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
            except TimeoutError:
                continue

    loop.__name__ = name
    return loop


def expiry_job(sessionmaker: async_sessionmaker[AsyncSession], interval_seconds: int = 5) -> Job:
    """XG-07 : expire les intents sans issue finale au bout de 15 minutes."""

    async def step() -> int:
        async with sessionmaker.begin() as session:
            return len(await expire_due_intents(session))

    return periodic("expire_intents", interval_seconds, step)


def idempotency_purge_job(
    sessionmaker: async_sessionmaker[AsyncSession], interval_seconds: int = 600
) -> Job:
    """Supprime les clés d'idempotence de plus de 24 h."""

    async def step() -> int:
        async with sessionmaker.begin() as session:
            return await purge_expired(session)

    return periodic("purge_idempotency_keys", interval_seconds, step)
