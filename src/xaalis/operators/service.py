"""Traitement d'un callback opérateur vérifié : déduplication, transition, grand livre.

Tout se passe dans **une seule transaction** : journal du callback, verrou de l'intent,
transition, écritures comptables et événement webhook. Un callback rejoué (Teranga Money
envoie parfois deux fois le même) heurte l'unicité ``(operator, operator_reference)`` et
n'a aucun effet.
"""

from __future__ import annotations

from enum import StrEnum

from sqlalchemy import update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from xaalis.core.errors import NotFoundError
from xaalis.core.fees import FeeBreakdown
from xaalis.core.operators import Operator
from xaalis.ledger import recipes
from xaalis.ledger import service as ledger
from xaalis.ledger.lines import TransactionKind
from xaalis.operators.callbacks import CallbackOutcome, NormalizedCallback
from xaalis.operators.models import OperatorCallback, ReconciliationAlert
from xaalis.payments import service as payments
from xaalis.payments.models import PaymentIntent
from xaalis.payments.state_machine import IntentStatus


class Disposition(StrEnum):
    APPLIED = "applied"
    DUPLICATE = "duplicate"
    LATE = "late_callback"  # XG-07 : après expiration
    UNEXPECTED = "unexpected_callback"  # intent dans un état final ou non confirmé
    OPERATOR_MISMATCH = "operator_mismatch"  # callback d'un opérateur qui n'est plus le bon
    AMOUNT_MISMATCH = "amount_mismatch"
    UNKNOWN_INTENT = "unknown_intent"


async def _alert(
    session: AsyncSession,
    kind: Disposition,
    callback: NormalizedCallback,
    intent: PaymentIntent | None,
) -> None:
    session.add(
        ReconciliationAlert(
            kind=kind.value,
            operator=callback.operator.value,
            operator_reference=callback.operator_reference,
            intent_id=intent.id if intent else None,
            livemode=intent.livemode if intent else None,
            details={
                "outcome": callback.outcome.value,
                "callback_amount": callback.amount,
                "intent_amount": intent.amount if intent else None,
                "intent_status": intent.status if intent else None,
                "intent_operator": intent.operator if intent else None,
            },
        )
    )
    await session.flush()


async def _capture_in_ledger(session: AsyncSession, intent: PaymentIntent) -> None:
    """Paiement réussi : écriture en partie double avec la commission FIGÉE à la création."""
    assert intent.operator is not None  # noqa: S101 — garanti par la contrainte en base
    breakdown = FeeBreakdown(amount=intent.amount, fee=intent.fee, net=intent.net)
    await ledger.post(
        session,
        livemode=intent.livemode,
        kind=TransactionKind.PAYMENT_CAPTURE,
        reference=intent.id,
        lines=recipes.payment_capture(intent.merchant_id, Operator(intent.operator), breakdown),
        description=f"Paiement {intent.id} via {intent.operator}",
    )


async def handle_callback(session: AsyncSession, callback: NormalizedCallback) -> Disposition:
    inserted = await session.scalar(
        pg_insert(OperatorCallback)
        .values(
            operator=callback.operator.value,
            operator_reference=callback.operator_reference,
            intent_id=callback.intent_id[:40],
            outcome=callback.outcome.value,
            amount=callback.amount,
            payload=callback.payload,
            disposition="received",
        )
        .on_conflict_do_nothing(index_elements=["operator", "operator_reference"])
        .returning(OperatorCallback.id)
    )
    if inserted is None:
        return Disposition.DUPLICATE

    disposition = await _apply(session, callback)
    await session.execute(
        update(OperatorCallback)
        .where(OperatorCallback.id == inserted)
        .values(disposition=disposition.value)
    )
    return disposition


async def _apply(session: AsyncSession, callback: NormalizedCallback) -> Disposition:
    try:
        intent = await payments.lock_intent(session, callback.intent_id)
    except NotFoundError:
        await _alert(session, Disposition.UNKNOWN_INTENT, callback, None)
        return Disposition.UNKNOWN_INTENT

    status = IntentStatus(intent.status)
    if status is IntentStatus.EXPIRED:
        await _alert(session, Disposition.LATE, callback, intent)
        return Disposition.LATE
    if status is not IntentStatus.PROCESSING:
        await _alert(session, Disposition.UNEXPECTED, callback, intent)
        return Disposition.UNEXPECTED
    if intent.operator != callback.operator.value:
        await _alert(session, Disposition.OPERATOR_MISMATCH, callback, intent)
        return Disposition.OPERATOR_MISMATCH
    if callback.amount != intent.amount:
        await _alert(session, Disposition.AMOUNT_MISMATCH, callback, intent)
        return Disposition.AMOUNT_MISMATCH

    succeeded = callback.outcome is CallbackOutcome.SUCCEEDED
    await payments.apply_operator_outcome(
        session, intent, succeeded=succeeded, operator_reference=callback.operator_reference
    )
    if succeeded:
        await _capture_in_ledger(session, intent)
    return Disposition.APPLIED
