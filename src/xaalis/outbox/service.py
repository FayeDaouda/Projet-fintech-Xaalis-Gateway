"""Outbox : écriture dans la transaction métier, puis relève par bail (lease).

Relève : ``claim`` réserve un lot de messages prêts pour une durée limitée (``locked_until``)
dans une transaction **courte**, puis le worker traite chaque message (appel réseau) hors
transaction, et termine par ``complete`` ou ``retry_later``. Un worker qui meurt en
cours de route laisse expirer son bail : le message est repris par un autre.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import timedelta
from typing import Any

import structlog
from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from xaalis.core.ids import new_id
from xaalis.observability.tracing import current_traceparent
from xaalis.outbox.models import OutboxMessage

# Préfixe des messages internes (commandes) : jamais livrés aux marchands.
INTERNAL_PREFIX = "internal."


async def enqueue(
    session: AsyncSession,
    *,
    topic: str,
    merchant_id: str,
    livemode: bool,
    aggregate_id: str,
    payload: dict[str, Any],
) -> str:
    """Ajoute un message à l'outbox, dans la transaction courante ; renvoie son ``evt_…``."""
    event_id = new_id("evt")
    # Le request_id est propagé jusqu'au worker et aux appels opérateurs (observabilité).
    request_id = structlog.contextvars.get_contextvars().get("request_id")
    session.add(
        OutboxMessage(
            event_id=event_id,
            topic=topic,
            merchant_id=merchant_id,
            livemode=livemode,
            aggregate_id=aggregate_id,
            payload=payload,
            request_id=request_id if isinstance(request_id, str) else None,
            # La trace de la requête traverse l'outbox avec l'événement (ADR 0014).
            traceparent=current_traceparent(),
        )
    )
    await session.flush()
    return event_id


async def claim(
    session: AsyncSession, *, topics: Sequence[str], limit: int, lease_seconds: int = 30
) -> list[OutboxMessage]:
    ready = (
        select(OutboxMessage.id)
        .where(
            OutboxMessage.published_at.is_(None),
            OutboxMessage.topic.in_(topics),
            OutboxMessage.available_at <= func.now(),
            or_(OutboxMessage.locked_until.is_(None), OutboxMessage.locked_until < func.now()),
        )
        .order_by(OutboxMessage.id)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    rows = await session.scalars(
        update(OutboxMessage)
        .where(OutboxMessage.id.in_(ready.scalar_subquery()))
        .values(
            locked_until=func.now() + timedelta(seconds=lease_seconds),
            attempts=OutboxMessage.attempts + 1,
        )
        .returning(OutboxMessage)
        .execution_options(synchronize_session=False)
    )
    return sorted(rows.all(), key=lambda message: message.id)


async def complete(session: AsyncSession, message_id: int) -> None:
    await session.execute(
        update(OutboxMessage)
        .where(OutboxMessage.id == message_id)
        .values(published_at=func.now(), locked_until=None, last_error=None)
    )


async def retry_later(
    session: AsyncSession,
    message_id: int,
    *,
    delay_seconds: int,
    error: str,
    payload_patch: dict[str, Any] | None = None,
) -> None:
    values: dict[str, Any] = {
        "available_at": func.now() + timedelta(seconds=delay_seconds),
        "locked_until": None,
        "last_error": error[:500],
    }
    if payload_patch:
        values["payload"] = OutboxMessage.payload.concat(payload_patch)
    await session.execute(
        update(OutboxMessage).where(OutboxMessage.id == message_id).values(**values)
    )
