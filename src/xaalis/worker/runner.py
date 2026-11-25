"""Boucle principale du worker : exécute des tâches périodiques jusqu'au signal d'arrêt.

Chaque tâche (relais de l'outbox, expiration des intents, rapprochement…) est une
coroutine ``async def job(stop: asyncio.Event) -> None`` qui doit rendre la main
rapidement quand ``stop`` est positionné. Le worker écrit un battement de cœur à
intervalle régulier : la sonde Docker vérifie sa fraîcheur.
"""

from __future__ import annotations

import asyncio
import signal
from collections.abc import Callable, Coroutine, Sequence
from pathlib import Path
from typing import Any

from xaalis.observability.logging import get_logger

Job = Callable[[asyncio.Event], Coroutine[Any, Any, None]]

HEARTBEAT_INTERVAL_SECONDS = 5.0
_log = get_logger(__name__)


async def heartbeat(stop: asyncio.Event, path: Path) -> None:
    while not stop.is_set():
        await asyncio.to_thread(path.touch)
        try:
            await asyncio.wait_for(stop.wait(), timeout=HEARTBEAT_INTERVAL_SECONDS)
        except TimeoutError:
            continue


async def run_jobs(jobs: Sequence[Job], stop: asyncio.Event) -> None:
    """Lance les tâches en parallèle ; si l'une plante, on arrête tout (fail fast)."""
    async with asyncio.TaskGroup() as group:
        for job in jobs:
            group.create_task(job(stop), name=getattr(job, "__name__", "job"))


def install_signal_handlers(stop: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
