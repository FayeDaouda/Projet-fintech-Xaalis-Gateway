"""Rafraîchissement des métriques métier au moment de la lecture de ``/metrics``.

Les jauges sont recalculées depuis la source de vérité (PostgreSQL, Redis) plutôt que
tenues à jour en mémoire : elles restent justes quel que soit le processus qui a fait
le travail, et après un redémarrage. Une erreur ne doit jamais casser ``/metrics``.
"""

from __future__ import annotations

from sqlalchemy import case, func, select
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from xaalis.core.operators import Operator
from xaalis.observability.logging import get_logger
from xaalis.observability.metrics import (
    CIRCUIT_BREAKER_STATE,
    OUTBOX_PENDING,
    PAYMENT_INTENTS,
    RECONCILIATION_ALERTS,
    RECONCILIATION_DISCREPANCIES,
    WEBHOOK_DELIVERIES,
)
from xaalis.operators.models import ReconciliationAlert
from xaalis.outbox.models import OutboxMessage
from xaalis.outbox.service import INTERNAL_PREFIX
from xaalis.payments.models import PaymentIntent
from xaalis.reconciliation.models import ReconciliationLine, ReconciliationRun
from xaalis.routing.health import BreakerState, OperatorHealth
from xaalis.webhooks.models import WebhookDelivery

_log = get_logger(__name__)
_BREAKER_VALUE = {BreakerState.CLOSED: 0, BreakerState.HALF_OPEN: 1, BreakerState.OPEN: 2}


async def refresh_business_metrics(
    sessionmaker: async_sessionmaker[AsyncSession], health: OperatorHealth
) -> None:
    try:
        for operator in Operator:
            CIRCUIT_BREAKER_STATE.labels(operator=operator.value).set(
                _BREAKER_VALUE[await health.state(operator)]
            )
        async with sessionmaker() as session:
            intents = await session.execute(
                select(
                    PaymentIntent.status,
                    func.coalesce(PaymentIntent.operator, "none"),
                    func.count(),
                ).group_by(PaymentIntent.status, PaymentIntent.operator)
            )
            family = case(
                (OutboxMessage.topic.startswith(INTERNAL_PREFIX), "operator_commands"),
                else_="webhooks",
            )
            pending = await session.execute(
                select(family, func.count())
                .where(OutboxMessage.published_at.is_(None))
                .group_by(family)
            )
            alerts = await session.execute(
                select(ReconciliationAlert.kind, func.count())
                .where(ReconciliationAlert.resolved_at.is_(None))
                .group_by(ReconciliationAlert.kind)
            )
            PAYMENT_INTENTS.clear()
            for status, operator_name, count in intents:
                PAYMENT_INTENTS.labels(status=status, operator=operator_name).set(count)
            OUTBOX_PENDING.clear()
            for name in ("webhooks", "operator_commands"):
                OUTBOX_PENDING.labels(family=name).set(0)
            for name, count in pending:
                OUTBOX_PENDING.labels(family=name).set(count)
            deliveries = await session.execute(
                select(WebhookDelivery.status, func.count()).group_by(WebhookDelivery.status)
            )
            for name in ("pending", "delivered", "dead_letter"):
                WEBHOOK_DELIVERIES.labels(status=name).set(0)
            for name, count in deliveries:
                WEBHOOK_DELIVERIES.labels(status=name).set(count)
            latest = (
                select(ReconciliationRun.id)
                .where(ReconciliationRun.status == "completed")
                .ext(distinct_on(ReconciliationRun.operator))
                .order_by(
                    ReconciliationRun.operator,
                    ReconciliationRun.statement_date.desc(),
                    ReconciliationRun.created_at.desc(),
                )
                .scalar_subquery()
            )
            gaps = await session.execute(
                select(ReconciliationRun.operator, ReconciliationLine.outcome, func.count())
                .join(ReconciliationLine, ReconciliationLine.run_id == ReconciliationRun.id)
                .where(ReconciliationRun.id.in_(latest), ReconciliationLine.outcome != "matched")
                .group_by(ReconciliationRun.operator, ReconciliationLine.outcome)
            )
            RECONCILIATION_DISCREPANCIES.clear()
            for gap_operator, outcome, count in gaps:
                RECONCILIATION_DISCREPANCIES.labels(operator=gap_operator, type=outcome).set(count)
            RECONCILIATION_ALERTS.clear()
            for kind, count in alerts:
                RECONCILIATION_ALERTS.labels(kind=kind).set(count)
    except Exception as exc:  # noqa: BLE001 — /metrics doit répondre même en mode dégradé
        _log.warning("business_metrics_refresh_failed", error=type(exc).__name__)
