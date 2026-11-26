"""Cas d'usage des intents : création, confirmation, annulation, expiration, lecture.

Toute modification d'état passe par ``_transition`` : verrou de l'intent (ADR 0002),
table de transitions, puis — dans la même transaction — historique (``payment_events``)
et événement dans l'outbox. Les fonctions ne valident jamais : l'appelant possède la
transaction.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from xaalis.core.errors import DomainError, NotFoundError
from xaalis.core.fees import fee_breakdown
from xaalis.core.ids import new_id
from xaalis.core.money import CURRENCY, ensure_intent_amount
from xaalis.core.operators import Operator
from xaalis.core.phone import normalize_phone
from xaalis.outbox.service import INTERNAL_PREFIX, enqueue
from xaalis.payments.models import PaymentEvent, PaymentIntent
from xaalis.payments.state_machine import (
    EXPIRABLE_STATUSES,
    IntentEvent,
    IntentStatus,
    next_status,
)
from xaalis.payments.views import PaymentIntentView
from xaalis.routing.catalog import RoutingSnapshot, plan_route

INTENT_TTL: Final = timedelta(minutes=15)  # XG-07
MAX_PROCESSING_PER_PAYER: Final = 3  # XG-14
OPERATOR_CHARGE_TOPIC: Final = f"{INTERNAL_PREFIX}operator.charge"
PAYMENT_EVENTS_CHANNEL: Final = "xaalis_payment_events"


class UnsupportedCurrencyError(DomainError):
    code = "unsupported_currency"


class PayerLimitExceededError(DomainError):
    code = "payer_processing_limit"


class IntentExpiredError(DomainError):
    code = "intent_expired"


@dataclass(frozen=True, slots=True)
class Scope:
    """Environnement d'un marchand : toute lecture et écriture y est confinée (XG-12)."""

    merchant_id: str
    livemode: bool


async def db_now(session: AsyncSession) -> datetime:
    # now() est figé au début de la transaction : une seule horloge, celle de la base.
    now: datetime | None = await session.scalar(select(func.now()))
    assert now is not None  # noqa: S101 — now() ne renvoie jamais NULL
    return now


async def _history(
    session: AsyncSession,
    intent: PaymentIntent,
    *,
    from_status: IntentStatus | None,
    cause: str,
    actor: str,
    details: Mapping[str, Any] | None = None,
) -> PaymentEvent:
    """Historise un fait sur l'intent (transition ou non), sans le publier."""
    event = PaymentEvent(
        intent_id=intent.id,
        from_status=from_status.value if from_status else None,
        to_status=intent.status,
        cause=cause,
        actor=actor,
        details=dict(details or {}),
    )
    session.add(event)
    await session.flush()
    return event


async def _record(
    session: AsyncSession,
    intent: PaymentIntent,
    *,
    from_status: IntentStatus | None,
    cause: str,
    actor: str,
    details: Mapping[str, Any] | None = None,
) -> None:
    """Historise un changement d'état ET le publie (outbox + NOTIFY), même transaction."""
    event = await _history(
        session, intent, from_status=from_status, cause=cause, actor=actor, details=details
    )
    # Flux SSE (lot 7) : NOTIFY est transactionnel, il n'est délivré qu'au COMMIT.
    await session.execute(
        select(
            func.pg_notify(
                PAYMENT_EVENTS_CHANNEL,
                json.dumps({"intent_id": intent.id, "event_id": event.id, "status": intent.status}),
            )
        )
    )
    await enqueue(
        session,
        topic=f"payment.{intent.status}",
        merchant_id=intent.merchant_id,
        livemode=intent.livemode,
        aggregate_id=intent.id,
        payload=PaymentIntentView.of(intent).model_dump(mode="json"),
    )


async def _transition(
    session: AsyncSession,
    intent: PaymentIntent,
    event: IntentEvent,
    *,
    actor: str,
    details: Mapping[str, Any] | None = None,
    **changes: Any,
) -> PaymentIntent:
    """Applique une transition à un intent **déjà verrouillé**."""
    previous = IntentStatus(intent.status)
    intent.status = next_status(previous, event).value
    for name, value in changes.items():
        setattr(intent, name, value)
    intent.updated_at = await db_now(session)
    await session.flush()
    await _record(
        session, intent, from_status=previous, cause=event.value, actor=actor, details=details
    )
    return intent


async def lock_intent(
    session: AsyncSession, intent_id: str, scope: Scope | None = None
) -> PaymentIntent:
    """``SELECT … FOR NO KEY UPDATE`` : sérialise toutes les transitions d'un même intent,
    sans bloquer les vérifications de clés étrangères des tables qui le référencent."""
    query = (
        select(PaymentIntent).where(PaymentIntent.id == intent_id).with_for_update(key_share=True)
    )
    if scope is not None:
        query = query.where(
            PaymentIntent.merchant_id == scope.merchant_id,
            PaymentIntent.livemode == scope.livemode,
        )
    intent = await session.scalar(query)
    if intent is None:
        # 404 et non 403 : on ne révèle pas l'existence d'un objet d'un autre marchand.
        raise NotFoundError("intent de paiement introuvable")
    return intent


# --------------------------------------------------------------------------- commandes


async def create_intent(
    session: AsyncSession,
    scope: Scope,
    *,
    amount: int,
    currency: str,
    payer_phone: str,
    metadata: Mapping[str, str] | None = None,
    description: str | None = None,
    actor: str,
) -> PaymentIntent:
    if currency != CURRENCY:
        msg = f"devise non prise en charge : {currency} (seul {CURRENCY} est accepté)"
        raise UnsupportedCurrencyError(msg)
    breakdown = fee_breakdown(ensure_intent_amount(amount))
    # Horodatages calculés par PostgreSQL dans l'INSERT et relus par RETURNING : pas
    # d'aller-retour « SELECT now() » de plus (cf. docs/load-test.md).
    intent = PaymentIntent(
        id=new_id("pi"),
        merchant_id=scope.merchant_id,
        livemode=scope.livemode,
        amount=breakdown.amount,
        currency=CURRENCY,
        fee=breakdown.fee,
        status=IntentStatus.CREATED.value,
        payer_phone=normalize_phone(payer_phone),
        amount_refunded=0,
        description=description,
        metadata_=dict(metadata or {}),
    )
    session.add(intent)
    await session.flush()
    await _record(session, intent, from_status=None, cause="create", actor=actor)
    return intent


async def confirm_intent(
    session: AsyncSession,
    scope: Scope,
    intent_id: str,
    *,
    actor: str,
    snapshot: RoutingSnapshot | None = None,
) -> PaymentIntent:
    """``created → processing`` : choisit l'opérateur et demande le débit (asynchrone).

    L'appel à l'opérateur n'est **pas** fait ici : une commande est écrite dans l'outbox
    et le worker l'exécute après le commit, hors de tout verrou (ADR 0002, ADR 0003).
    """
    intent = await lock_intent(session, intent_id, scope)
    next_status(IntentStatus(intent.status), IntentEvent.CONFIRM)  # 409 avant tout autre contrôle
    if await db_now(session) >= intent.expires_at:
        raise IntentExpiredError("l'intent a expiré et ne peut plus être confirmé")

    # XG-14 : verrou consultatif par payeur (ordre : intent puis payeur), puis comptage.
    await session.execute(
        select(func.pg_advisory_xact_lock(func.hashtextextended(f"payer:{intent.payer_phone}", 0)))
    )
    processing = await session.scalar(
        select(func.count()).where(
            PaymentIntent.payer_phone == intent.payer_phone,
            PaymentIntent.livemode == intent.livemode,
            PaymentIntent.status == IntentStatus.PROCESSING.value,
        )
    )
    if (processing or 0) >= MAX_PROCESSING_PER_PAYER:
        msg = f"le payeur a déjà {MAX_PROCESSING_PER_PAYER} paiements en cours"
        raise PayerLimitExceededError(msg)

    candidates = plan_route(intent.payer_phone, snapshot)
    await _transition(
        session,
        intent,
        IntentEvent.CONFIRM,
        actor=actor,
        details={"candidates": [op.value for op in candidates]},
        operator=candidates[0].value,
    )
    await enqueue(
        session,
        topic=OPERATOR_CHARGE_TOPIC,
        merchant_id=intent.merchant_id,
        livemode=intent.livemode,
        aggregate_id=intent.id,
        payload={"intent_id": intent.id, "candidates": [op.value for op in candidates]},
    )
    return intent


async def select_operator(
    session: AsyncSession, intent_id: str, operator: Operator, *, actor: str
) -> PaymentIntent | None:
    """Avant chaque tentative d'envoi : fixe l'opérateur courant (bascule éventuelle).

    Renvoie ``None`` si l'intent n'est plus ``processing`` (expiré, annulé…) : il ne faut
    alors plus rien envoyer. Fixer l'opérateur AVANT l'appel garantit qu'un callback
    rapide de cet opérateur trouve l'intent déjà associé à lui.
    """
    intent = await lock_intent(session, intent_id)
    if intent.status != IntentStatus.PROCESSING.value:
        return None
    if intent.operator != operator.value:
        previous = intent.operator
        intent.operator = operator.value
        intent.updated_at = await db_now(session)
        await session.flush()
        await _history(
            session,
            intent,
            from_status=IntentStatus.PROCESSING,
            cause="operator_failover",
            actor=actor,
            details={"from": previous, "to": operator.value},
        )
    return intent


async def apply_risk_verdict(
    session: AsyncSession,
    intent_id: str,
    *,
    decision: str,
    source: str,
    score_permille: int | None,
    reasons: Sequence[str],
    actor: str,
) -> PaymentIntent:
    """Enregistre l'évaluation anti-fraude, **une seule fois**, avant le premier débit.

    ``decline`` fait échouer l'intent (``processing → failed``, cause ``risk_declined``) :
    webhook ``payment.failed``, aucun débit demandé, rien au grand livre. Sinon, le fait est
    historisé sans changement d'état. Un intent qui n'est plus ``processing`` ou déjà évalué
    est renvoyé tel quel : l'appelant décide de la suite d'après son statut.
    """
    intent = await lock_intent(session, intent_id)
    if intent.status != IntentStatus.PROCESSING.value or intent.risk_decision is not None:
        return intent
    details = {
        "score_permille": score_permille,
        "decision": decision,
        "source": source,
        "reasons": list(reasons),
    }
    changes = {"risk_score": score_permille, "risk_decision": decision, "risk_source": source}
    if decision == "decline":
        return await _transition(
            session, intent, IntentEvent.RISK_DECLINED, actor=actor, details=details, **changes
        )
    for name, value in changes.items():
        setattr(intent, name, value)
    intent.updated_at = await db_now(session)
    await session.flush()
    await _history(
        session,
        intent,
        from_status=IntentStatus.PROCESSING,
        cause="risk_assessed",
        actor=actor,
        details=details,
    )
    return intent


async def record_operator_acceptance(
    session: AsyncSession, intent_id: str, operator: Operator, operator_reference: str
) -> None:
    """L'opérateur a accepté la demande de débit : on retient sa référence."""
    intent = await lock_intent(session, intent_id)
    if intent.operator == operator.value and intent.operator_reference is None:
        intent.operator_reference = operator_reference
        await session.flush()


async def apply_operator_outcome(
    session: AsyncSession,
    intent: PaymentIntent,
    *,
    succeeded: bool,
    operator_reference: str,
) -> PaymentIntent:
    """Issue finale annoncée par l'opérateur, sur un intent **déjà verrouillé**."""
    if succeeded:
        return await _transition(
            session,
            intent,
            IntentEvent.OPERATOR_SUCCEEDED,
            actor=f"operator:{intent.operator}",
            operator_reference=operator_reference,
            succeeded_at=await db_now(session),
        )
    return await _transition(
        session,
        intent,
        IntentEvent.OPERATOR_FAILED,
        actor=f"operator:{intent.operator}",
        operator_reference=operator_reference,
    )


async def apply_refund(
    session: AsyncSession,
    intent: PaymentIntent,
    event: IntentEvent,
    *,
    amount: int,
    actor: str,
    refund_id: str,
) -> PaymentIntent:
    """Transition de remboursement sur un intent **déjà verrouillé** (XG-09 vérifiée avant)."""
    return await _transition(
        session,
        intent,
        event,
        actor=actor,
        details={"refund_id": refund_id, "amount": amount},
        amount_refunded=intent.amount_refunded + amount,
    )


async def cancel_intent(
    session: AsyncSession, scope: Scope, intent_id: str, *, actor: str
) -> PaymentIntent:
    intent = await lock_intent(session, intent_id, scope)
    next_status(IntentStatus(intent.status), IntentEvent.CANCEL)
    if await db_now(session) >= intent.expires_at:
        raise IntentExpiredError("l'intent a expiré et ne peut plus être annulé")
    return await _transition(session, intent, IntentEvent.CANCEL, actor=actor)


async def expire_due_intents(session: AsyncSession, *, batch_size: int = 100) -> list[str]:
    """XG-07 : passe en ``expired`` les intents sans issue finale après 15 minutes.

    ``FOR UPDATE SKIP LOCKED`` : plusieurs workers se partagent le travail sans attente,
    et un intent verrouillé par un callback en cours est simplement repris au tour suivant.
    """
    intents = (
        await session.scalars(
            select(PaymentIntent)
            .where(
                PaymentIntent.status.in_([s.value for s in EXPIRABLE_STATUSES]),
                PaymentIntent.expires_at <= func.now(),
            )
            .order_by(PaymentIntent.expires_at)
            .limit(batch_size)
            .with_for_update(skip_locked=True)
        )
    ).all()
    for intent in intents:
        await _transition(session, intent, IntentEvent.EXPIRE, actor="system:expiry")
    return [intent.id for intent in intents]


# --------------------------------------------------------------------------- lectures


async def get_intent(session: AsyncSession, scope: Scope, intent_id: str) -> PaymentIntent:
    intent = await session.scalar(
        select(PaymentIntent).where(
            PaymentIntent.id == intent_id,
            PaymentIntent.merchant_id == scope.merchant_id,
            PaymentIntent.livemode == scope.livemode,
        )
    )
    if intent is None:
        raise NotFoundError("intent de paiement introuvable")
    return intent


async def list_intents(
    session: AsyncSession,
    scope: Scope,
    *,
    status: IntentStatus | None,
    limit: int,
    starting_after: str | None,
) -> tuple[Sequence[PaymentIntent], bool]:
    query = (
        select(PaymentIntent)
        .where(
            PaymentIntent.merchant_id == scope.merchant_id,
            PaymentIntent.livemode == scope.livemode,
        )
        .order_by(PaymentIntent.id.desc())
        .limit(limit + 1)
    )
    if status is not None:
        query = query.where(PaymentIntent.status == status.value)
    if starting_after is not None:
        query = query.where(PaymentIntent.id < starting_after)
    rows = (await session.scalars(query)).all()
    return rows[:limit], len(rows) > limit


async def history(session: AsyncSession, intent_id: str) -> Sequence[PaymentEvent]:
    return (
        await session.scalars(
            select(PaymentEvent)
            .where(PaymentEvent.intent_id == intent_id)
            .order_by(PaymentEvent.id)
        )
    ).all()
