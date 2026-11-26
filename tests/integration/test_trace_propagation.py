"""Bonus OpenTelemetry (ADR 0014) : le contexte de trace est écrit avec l'événement, dans la
même transaction, puis suit le webhook (outbox → webhook_events)."""

from __future__ import annotations

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.integration.conftest import random_phone
from tests.support import MerchantFactory
from xaalis.observability.tracing import TRACER, current_traceparent
from xaalis.outbox.models import OutboxMessage
from xaalis.payments.service import Scope, cancel_intent, create_intent
from xaalis.webhooks.models import WebhookEvent
from xaalis.webhooks.service import fan_out


async def test_traceparent_travels_with_the_outbox_message_to_the_webhook_event(
    sessionmaker: async_sessionmaker[AsyncSession], make_merchant: MerchantFactory
) -> None:
    if not isinstance(trace.get_tracer_provider(), TracerProvider):
        trace.set_tracer_provider(TracerProvider())
    scope = Scope(await make_merchant(), False)
    async with sessionmaker.begin() as session:
        intent = await create_intent(
            session, scope, amount=5_000, currency="XOF", payer_phone=random_phone(), actor="t"
        )
    with TRACER.start_as_current_span("POST /cancel"):
        expected = current_traceparent()
        async with sessionmaker.begin() as session:
            await cancel_intent(session, scope, intent.id, actor="t")

    async with sessionmaker() as session:
        message = await session.scalar(
            select(OutboxMessage).where(
                OutboxMessage.aggregate_id == intent.id, OutboxMessage.topic == "payment.canceled"
            )
        )
    assert message is not None
    assert expected is not None
    assert message.traceparent == expected

    async with sessionmaker.begin() as session:
        while await fan_out(session):
            pass
    async with sessionmaker() as session:
        event = await session.get(WebhookEvent, message.event_id)
    assert event is not None
    assert event.traceparent == expected
