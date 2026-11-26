"""Tâches périodiques du worker.

Chaque tâche est une boucle « exécuter un lot, puis attendre » qui s'arrête proprement
dès que l'événement ``stop`` est positionné. Une erreur sur un lot est journalisée et la
boucle continue : une panne transitoire de la base ne doit pas tuer le worker.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import date, timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from xaalis.core.clock import today_in_dakar
from xaalis.core.operators import Operator
from xaalis.idempotency.service import purge_expired
from xaalis.observability.logging import get_logger
from xaalis.operators.dispatcher import Dispatcher
from xaalis.payments.service import expire_due_intents
from xaalis.reconciliation import service as reconciliation
from xaalis.settlement.service import settle_day
from xaalis.webhooks.delivery import WebhookDeliverer
from xaalis.webhooks.service import fan_out
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


def dispatch_job(dispatcher: Dispatcher, interval_seconds: int = 1) -> Job:
    """Lot 3 : relaie les demandes de débit de l'outbox vers les opérateurs."""

    async def step() -> int:
        return await dispatcher.run_batch()

    return periodic("dispatch_operator_charges", interval_seconds, step)


def webhook_fanout_job(
    sessionmaker: async_sessionmaker[AsyncSession], interval_seconds: int = 1
) -> Job:
    """Lot 5 : outbox → événements et livraisons par endpoint abonné."""

    async def step() -> int:
        async with sessionmaker.begin() as session:
            return await fan_out(session)

    return periodic("webhook_fanout", interval_seconds, step)


def webhook_delivery_job(deliverer: WebhookDeliverer, interval_seconds: int = 1) -> Job:
    """Lot 5 : envoie les webhooks dus, planifie les relances, bascule en dead letter."""

    async def step() -> int:
        return await deliverer.run_batch()

    return periodic("webhook_delivery", interval_seconds, step)


def reconciliation_job(
    sessionmaker: async_sessionmaker[AsyncSession],
    fetch: reconciliation.StatementFetcher,
    interval_seconds: int = 2,
) -> Job:
    """Lot 6 : traite les demandes de rapprochement en attente."""

    async def step() -> int:
        done = 0
        while await reconciliation.process_next(sessionmaker, fetch):
            done += 1
        return done

    return periodic("reconciliation", interval_seconds, step)


def nightly_job(sessionmaker: async_sessionmaker[AsyncSession], interval_seconds: int = 300) -> Job:
    """Chaque nuit (heure de Dakar) : rapprochement de la veille, puis règlement T+1.

    Idempotent : la demande n'est créée qu'une fois par jour ; le règlement ne traite que
    les paiements rapprochés sans écart et pas encore réglés.
    """

    async def step() -> int:
        return await run_nightly(sessionmaker, today_in_dakar())

    return periodic("nightly_reconciliation_settlement", interval_seconds, step)


async def run_nightly(sessionmaker: async_sessionmaker[AsyncSession], today: date) -> int:
    """Demande le rapprochement de la veille (une fois), puis règle ce qui est rapproché."""
    yesterday = today - timedelta(days=1)
    async with sessionmaker.begin() as session:
        if not await reconciliation.latest_runs(session, yesterday):
            await reconciliation.request_runs(
                session, yesterday, operators=list(Operator), requested_by="system:nightly"
            )
    summary = await settle_day(sessionmaker, yesterday, as_of=today)
    return summary.settled
