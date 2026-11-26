"""Webhooks : enregistrement des endpoints, diffusion depuis l'outbox, rejeu.

Diffusion (*fan-out*) : chaque message de l'outbox livrable devient un ``webhook_event``
(son ``evt_…`` est conservé) et une ``webhook_delivery`` par endpoint abonné, dans une
seule transaction qui marque aussi le message comme publié. Aucun appel réseau ici.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from xaalis.core.errors import NotFoundError
from xaalis.core.ids import new_id
from xaalis.outbox import service as outbox
from xaalis.payments.service import Scope
from xaalis.webhooks import crypto
from xaalis.webhooks.models import WebhookDelivery, WebhookEndpoint, WebhookEvent
from xaalis.webhooks.ssrf import validate_url


class EventType(StrEnum):
    PAYMENT_CREATED = "payment.created"
    PAYMENT_PROCESSING = "payment.processing"
    PAYMENT_SUCCEEDED = "payment.succeeded"
    PAYMENT_FAILED = "payment.failed"
    PAYMENT_CANCELED = "payment.canceled"
    PAYMENT_EXPIRED = "payment.expired"
    PAYMENT_PARTIALLY_REFUNDED = "payment.partially_refunded"
    PAYMENT_REFUNDED = "payment.refunded"
    REFUND_SUCCEEDED = "refund.succeeded"
    PAYOUT_PAID = "payout.paid"


DELIVERABLE_TOPICS = [event.value for event in EventType]


@dataclass(frozen=True, slots=True)
class CreatedEndpoint:
    endpoint: WebhookEndpoint
    secret: str  # affiché une seule fois


async def create_endpoint(
    session: AsyncSession,
    scope: Scope,
    *,
    url: str,
    events: Sequence[EventType],
    encryption_key: str,
    secret: str | None = None,
) -> CreatedEndpoint:
    validate_url(url, livemode=scope.livemode)  # OWASP API7 : SSRF
    raw_secret = secret or crypto.generate_secret()
    endpoint = WebhookEndpoint(
        id=new_id("we"),
        merchant_id=scope.merchant_id,
        livemode=scope.livemode,
        url=url,
        events=sorted({event.value for event in events}),
        secret_ciphertext=crypto.encrypt(encryption_key, raw_secret),
        enabled=True,
    )
    session.add(endpoint)
    await session.flush()
    await session.refresh(endpoint, ["created_at"])
    return CreatedEndpoint(endpoint=endpoint, secret=raw_secret)


async def fan_out(session: AsyncSession, *, limit: int = 100) -> int:
    """Transforme les messages livrables de l'outbox en livraisons, atomiquement."""
    messages = await outbox.claim(session, topics=DELIVERABLE_TOPICS, limit=limit)
    for message in messages:
        await session.execute(
            pg_insert(WebhookEvent)
            .values(
                id=message.event_id,
                merchant_id=message.merchant_id,
                livemode=message.livemode,
                type=message.topic,
                payload=message.payload,
                request_id=message.request_id,
                traceparent=message.traceparent,
                created_at=message.created_at,
            )
            .on_conflict_do_nothing(index_elements=["id"])
        )
        endpoints = await session.scalars(
            select(WebhookEndpoint.id).where(
                WebhookEndpoint.merchant_id == message.merchant_id,
                WebhookEndpoint.livemode == message.livemode,
                WebhookEndpoint.enabled.is_(True),
                WebhookEndpoint.events.contains([message.topic]),  # opérateur @> sur ARRAY
            )
        )
        endpoint_ids = list(endpoints)
        if endpoint_ids:
            await session.execute(
                pg_insert(WebhookDelivery)
                .values([{"event_id": message.event_id, "endpoint_id": e} for e in endpoint_ids])
                .on_conflict_do_nothing(index_elements=["event_id", "endpoint_id"])
            )
        await outbox.complete(session, message.id)
    return len(messages)


async def get_event(
    session: AsyncSession, scope: Scope, event_id: str
) -> tuple[WebhookEvent, Sequence[WebhookDelivery]]:
    event = await session.scalar(
        select(WebhookEvent).where(
            WebhookEvent.id == event_id,
            WebhookEvent.merchant_id == scope.merchant_id,
            WebhookEvent.livemode == scope.livemode,
        )
    )
    if event is None:
        raise NotFoundError("événement introuvable")
    deliveries = (
        await session.scalars(
            select(WebhookDelivery)
            .where(WebhookDelivery.event_id == event_id)
            .order_by(WebhookDelivery.id)
        )
    ).all()
    return event, deliveries


async def replay(session: AsyncSession, scope: Scope, event_id: str) -> int:
    """Renvoie un événement à tous ses endpoints, y compris depuis la dead letter."""
    await get_event(session, scope, event_id)  # 404 si l'événement n'est pas à ce marchand
    result = await session.execute(
        update(WebhookDelivery)
        .where(WebhookDelivery.event_id == event_id)
        .values(
            status="pending",
            attempts=0,
            next_attempt_at=func.now(),
            locked_until=None,
            last_error=None,
        )
    )
    return int(getattr(result, "rowcount", 0) or 0)
