"""Remboursement d'un intent (XG-08 à XG-11). Voir ADR 0008.

Ordre des verrous (ADR 0002) : **environnement marchand**, puis **intent**. Le premier
sérialise toutes les opérations qui dépendent du solde du marchand (XG-11) ; le second,
celles qui dépendent du montant déjà remboursé (XG-09). Sous ces deux verrous, les
contrôles sont exacts : 50 demandes simultanées de 1 000 sur 10 000 → 10 acceptées.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from xaalis.core.errors import DomainError
from xaalis.core.ids import new_id
from xaalis.core.money import ensure_integer_amount
from xaalis.core.operators import Operator
from xaalis.ledger import recipes
from xaalis.ledger import service as ledger
from xaalis.ledger.lines import TransactionKind
from xaalis.merchants.models import MerchantEnvironment
from xaalis.outbox.service import enqueue
from xaalis.payments import service as payments
from xaalis.payments.state_machine import IntentEvent, IntentStatus, next_status
from xaalis.refunds.models import Refund
from xaalis.refunds.views import RefundView

REFUND_WINDOW: Final = timedelta(days=30)  # XG-08


class RefundWindowExpiredError(DomainError):
    code = "refund_window_expired"


class RefundExceedsAmountError(DomainError):
    code = "refund_exceeds_amount"


class InsufficientBalanceError(DomainError):
    code = "insufficient_balance"


class InvalidRefundAmountError(DomainError):
    code = "invalid_amount"


async def _lock_merchant_environment(session: AsyncSession, scope: payments.Scope) -> None:
    await session.execute(
        select(MerchantEnvironment.merchant_id)
        .where(
            MerchantEnvironment.merchant_id == scope.merchant_id,
            MerchantEnvironment.livemode == scope.livemode,
        )
        # FOR NO KEY UPDATE et non FOR UPDATE : les clés étrangères vers cette ligne posent
        # des verrous FOR KEY SHARE (idempotence, intents…) qui bloqueraient FOR UPDATE et
        # interbloqueraient les requêtes concurrentes. NO KEY UPDATE reste exclusif entre
        # remboursements, sans conflit avec ces vérifications (ADR 0002).
        .with_for_update(key_share=True)
    )


async def create_refund(
    session: AsyncSession,
    scope: payments.Scope,
    intent_id: str,
    *,
    amount: int | None,
    reason: str | None,
    actor: str,
) -> Refund:
    """Rembourse ``amount`` (par défaut : tout le reste remboursable) d'un intent."""
    await _lock_merchant_environment(session, scope)  # 1er verrou : solde du marchand
    intent = await payments.lock_intent(session, intent_id, scope)  # 2e verrou : l'intent

    # XG-08 : l'état doit permettre un remboursement. On interroge la table de transitions
    # (seule autorité) : 409 invalid_transition pour created, processing, failed, refunded…
    status = IntentStatus(intent.status)
    next_status(status, IntentEvent.REFUND_PARTIAL)

    remaining = intent.amount - intent.amount_refunded
    refund_amount = remaining if amount is None else ensure_integer_amount(amount)
    if refund_amount <= 0:
        raise InvalidRefundAmountError("le montant d'un remboursement est strictement positif")
    if refund_amount > remaining:
        # XG-09 : la somme des remboursements ne dépasse jamais le montant initial.
        msg = f"il reste {remaining} XOF remboursables sur cet intent"
        raise RefundExceedsAmountError(msg)
    event = IntentEvent.REFUND_FULL if refund_amount == remaining else IntentEvent.REFUND_PARTIAL

    # … et seulement dans les 30 jours suivant le succès.
    assert intent.succeeded_at is not None  # noqa: S101 — garanti par la contrainte en base
    if await payments.db_now(session) > intent.succeeded_at + REFUND_WINDOW:
        raise RefundWindowExpiredError("le délai de remboursement de 30 jours est dépassé")

    # XG-11 : le marchand doit pouvoir couvrir le remboursement (pending d'abord).
    balances = await ledger.merchant_balances(session, scope.merchant_id, livemode=scope.livemode)
    from_pending = min(refund_amount, max(balances.pending, 0))
    from_available = refund_amount - from_pending
    if from_available > max(balances.available, 0):
        msg = (
            f"solde insuffisant : {balances.pending + balances.available} XOF disponibles "
            f"pour un remboursement de {refund_amount} XOF"
        )
        raise InsufficientBalanceError(msg)

    refund_id = new_id("re")
    assert intent.operator is not None  # noqa: S101 — un intent réussi a un opérateur
    posting = await ledger.post(
        session,
        livemode=scope.livemode,
        kind=TransactionKind.REFUND,
        reference=refund_id,
        # XG-10 : la commission n'est pas extournée, xaalis_revenue n'est pas touché.
        lines=recipes.refund(
            scope.merchant_id,
            Operator(intent.operator),
            from_pending=from_pending,
            from_available=from_available,
        ),
        description=f"Remboursement {refund_id} de {intent.id}",
    )
    refund = Refund(
        id=refund_id,
        intent_id=intent.id,
        merchant_id=scope.merchant_id,
        livemode=scope.livemode,
        amount=refund_amount,
        from_pending=from_pending,
        from_available=from_available,
        status="succeeded",
        reason=reason,
        ledger_transaction_id=posting.transaction_id,
    )
    session.add(refund)
    await payments.apply_refund(
        session,
        intent,
        event,
        amount=refund_amount,
        actor=actor,
        refund_id=refund_id,
    )
    await session.flush()
    await session.refresh(refund, ["created_at"])
    await enqueue(
        session,
        topic="refund.succeeded",
        merchant_id=scope.merchant_id,
        livemode=scope.livemode,
        aggregate_id=refund_id,
        payload=RefundView.of(refund).model_dump(mode="json"),
    )
    return refund
