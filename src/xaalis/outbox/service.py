"""Écriture dans l'outbox (le relais vers les webhooks et les opérateurs est au lot 3/5)."""

from __future__ import annotations

from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from xaalis.core.ids import new_id
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
        )
    )
    await session.flush()
    return event_id
