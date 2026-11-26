"""Règlement T+1 (lot 6, XG-15) et virements.

Règlement du jour comptable J (Dakar), exécutable à partir de J+1 seulement : pour chaque
paiement comptabilisé le jour J et **rapproché sans écart**, on déplace de
``merchant_pending`` vers ``merchant_available`` ce que le marchand reste dû sur ce paiement
(net − déjà remboursé), sans jamais dépasser le ``pending`` réellement restant (ADR 0008).
Un paiement n'est réglé qu'une fois : la table ``settlements`` a l'intent pour clé primaire.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from xaalis.core.clock import business_day_bounds
from xaalis.core.errors import DomainError
from xaalis.core.ids import new_id
from xaalis.ledger import recipes
from xaalis.ledger import service as ledger
from xaalis.ledger.lines import TransactionKind
from xaalis.ledger.models import LedgerTransaction
from xaalis.merchants.models import MerchantEnvironment
from xaalis.outbox.service import enqueue
from xaalis.payments.models import PaymentIntent
from xaalis.payments.service import Scope
from xaalis.reconciliation.service import matched_intents
from xaalis.settlement.models import Payout, Settlement
from xaalis.settlement.views import PayoutView


class SettlementTooEarlyError(DomainError):
    code = "settlement_too_early"


class NothingToPayOutError(DomainError):
    code = "insufficient_balance"


@dataclass(slots=True)
class SettlementSummary:
    business_date: date
    settled: int = 0
    settled_amount: int = 0
    skipped_unreconciled: list[str] = field(default_factory=list)


async def _lock_merchant(session: AsyncSession, merchant_id: str, livemode: bool) -> None:
    await session.execute(
        select(MerchantEnvironment.merchant_id)
        .where(
            MerchantEnvironment.merchant_id == merchant_id,
            MerchantEnvironment.livemode == livemode,
        )
        .with_for_update(key_share=True)  # FOR NO KEY UPDATE (ADR 0002)
    )


async def settle_day(
    sessionmaker: async_sessionmaker[AsyncSession], business_date: date, *, as_of: date
) -> SettlementSummary:
    if business_date >= as_of:
        msg = f"règlement T+1 : les paiements du {business_date} se règlent à partir du lendemain"
        raise SettlementTooEarlyError(msg)
    start, end = business_day_bounds(business_date)
    summary = SettlementSummary(business_date=business_date)
    async with sessionmaker() as session:
        reconciled = await matched_intents(session, business_date)
        candidates = (
            await session.scalars(
                select(PaymentIntent.id)
                .join(LedgerTransaction, LedgerTransaction.reference == PaymentIntent.id)
                .outerjoin(Settlement, Settlement.intent_id == PaymentIntent.id)
                .where(
                    LedgerTransaction.kind == TransactionKind.PAYMENT_CAPTURE.value,
                    LedgerTransaction.created_at >= start,
                    LedgerTransaction.created_at < end,
                    Settlement.intent_id.is_(None),
                )
                .order_by(PaymentIntent.id)
            )
        ).all()

    for intent_id in candidates:
        run_id = reconciled.get(intent_id)
        if run_id is None:  # non rapproché, ou en écart : il attend
            summary.skipped_unreconciled.append(intent_id)
            continue
        amount = await _settle_one(sessionmaker, intent_id, business_date, run_id)
        if amount is not None:
            summary.settled += 1
            summary.settled_amount += amount
    return summary


async def _settle_one(
    sessionmaker: async_sessionmaker[AsyncSession], intent_id: str, day: date, run_id: str
) -> int | None:
    async with sessionmaker.begin() as session:
        intent = await session.get(PaymentIntent, intent_id)
        assert intent is not None  # noqa: S101 — issu de la requête précédente
        await _lock_merchant(session, intent.merchant_id, intent.livemode)
        balances = await ledger.merchant_balances(
            session, intent.merchant_id, livemode=intent.livemode
        )
        owed = max(intent.net - intent.amount_refunded, 0)
        amount = min(owed, max(balances.pending, 0))
        transaction_id = None
        if amount > 0:
            posting = await ledger.post(
                session,
                livemode=intent.livemode,
                kind=TransactionKind.SETTLEMENT,
                reference=intent.id,
                lines=recipes.settlement(intent.merchant_id, amount),
                description=f"Règlement T+1 du {day} pour {intent.id}",
            )
            transaction_id = posting.transaction_id
        inserted = await session.scalar(
            pg_insert(Settlement)
            .values(
                intent_id=intent.id,
                business_date=day,
                amount=amount,
                ledger_transaction_id=transaction_id,
                reconciliation_run_id=run_id,
            )
            .on_conflict_do_nothing(index_elements=["intent_id"])
            .returning(Settlement.intent_id)
        )
        if inserted is None:  # réglé entre-temps par un autre worker
            await session.rollback()
            return None
        return amount


async def create_payout(session: AsyncSession, scope: Scope, *, actor: str) -> Payout:
    """Vire tout le solde ``available`` vers ``merchant_payout`` (sous verrou marchand)."""
    await _lock_merchant(session, scope.merchant_id, scope.livemode)
    balances = await ledger.merchant_balances(session, scope.merchant_id, livemode=scope.livemode)
    if balances.available <= 0:
        raise NothingToPayOutError("aucun fonds disponible à virer (règlement T+1 en attente ?)")
    payout_id = new_id("po")
    posting = await ledger.post(
        session,
        livemode=scope.livemode,
        kind=TransactionKind.PAYOUT,
        reference=payout_id,
        lines=recipes.payout(scope.merchant_id, balances.available),
        description=f"Virement {payout_id} ({actor})",
    )
    payout = Payout(
        id=payout_id,
        merchant_id=scope.merchant_id,
        livemode=scope.livemode,
        amount=balances.available,
        status="paid",
        ledger_transaction_id=posting.transaction_id,
    )
    session.add(payout)
    await session.flush()
    await session.refresh(payout, ["created_at"])
    await enqueue(
        session,
        topic="payout.paid",
        merchant_id=scope.merchant_id,
        livemode=scope.livemode,
        aggregate_id=payout_id,
        payload=PayoutView.of(payout).model_dump(mode="json"),
    )
    return payout
