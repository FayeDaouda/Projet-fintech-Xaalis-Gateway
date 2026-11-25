"""Simulation d'activité comptable et modèle de référence, partagés par les tests.

Le modèle tient en trois entiers par marchand (pending, available, paid_out) mis à jour
par une arithmétique triviale ; le grand livre réel doit toujours lui être égal.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from hypothesis import strategies as st
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from xaalis.core.fees import fee_breakdown
from xaalis.core.ids import new_id
from xaalis.core.money import MAX_INTENT_AMOUNT, MIN_INTENT_AMOUNT
from xaalis.core.operators import Operator
from xaalis.ledger import recipes, service
from xaalis.ledger.lines import Line, TransactionKind
from xaalis.ledger.service import MerchantBalances


@dataclass(frozen=True)
class Capture:
    amount: int
    operator: Operator


@dataclass(frozen=True)
class Settle:
    pass


@dataclass(frozen=True)
class Refund:
    amount: int
    operator: Operator


@dataclass(frozen=True)
class Payout:
    pass


@dataclass(frozen=True)
class ReverseLast:
    pass


Operation = Capture | Settle | Refund | Payout | ReverseLast
Delta = tuple[int, int, int]  # (pending, available, paid_out)

_amounts = st.integers(MIN_INTENT_AMOUNT, MAX_INTENT_AMOUNT)
_operators = st.sampled_from(list(Operator))
operations = st.one_of(
    st.builds(Capture, _amounts, _operators),
    st.just(Settle()),
    st.builds(Refund, st.integers(1, 500_000), _operators),
    st.just(Payout()),
    st.just(ReverseLast()),
)


def random_operation(rng: random.Random) -> Operation:
    """Équivalent non-Hypothesis, pour les gros volumes (1 000 opérations)."""
    operator = rng.choice(list(Operator))
    roll = rng.random()
    if roll < 0.5:
        return Capture(rng.randint(MIN_INTENT_AMOUNT, 200_000), operator)
    if roll < 0.65:
        return Settle()
    if roll < 0.8:
        return Refund(rng.randint(1, 50_000), operator)
    if roll < 0.92:
        return Payout()
    return ReverseLast()


@dataclass
class MerchantModel:
    merchant_id: str
    pending: int = 0
    available: int = 0
    paid_out: int = 0
    history: list[tuple[str, Delta]] = field(default_factory=list)

    def expected(self) -> MerchantBalances:
        return MerchantBalances(self.pending, self.available, self.paid_out)

    def apply(self, delta: Delta) -> None:
        self.pending += delta[0]
        self.available += delta[1]
        self.paid_out += delta[2]


async def apply_operation(
    sessionmaker: async_sessionmaker[AsyncSession], model: MerchantModel, op: Operation
) -> None:
    """Applique une opération valide au grand livre et au modèle ; ignore les invalides."""
    m = model.merchant_id
    plan: tuple[TransactionKind, list[Line], Delta] | None = None

    match op:
        case Capture(amount, operator):
            b = fee_breakdown(amount)
            plan = (
                TransactionKind.PAYMENT_CAPTURE,
                recipes.payment_capture(m, operator, b),
                (
                    b.net,
                    0,
                    0,
                ),
            )
        case Settle() if model.pending > 0:
            x = model.pending
            plan = TransactionKind.SETTLEMENT, recipes.settlement(m, x), (-x, x, 0)
        case Refund(amount, operator):
            from_pending = min(amount, max(model.pending, 0))
            from_available = amount - from_pending
            if from_available <= max(model.available, 0):
                lines = recipes.refund(
                    m, operator, from_pending=from_pending, from_available=from_available
                )
                plan = TransactionKind.REFUND, lines, (-from_pending, -from_available, 0)
        case Payout() if model.available > 0:
            x = model.available
            plan = TransactionKind.PAYOUT, recipes.payout(m, x), (0, -x, x)
        case ReverseLast() if model.history:
            transaction_id, delta = model.history.pop()
            async with sessionmaker.begin() as session:
                await service.reverse(session, transaction_id, reason="simulation")
            model.apply((-delta[0], -delta[1], -delta[2]))
            return
        case _:
            return

    if plan is None:
        return
    kind, lines, delta = plan
    async with sessionmaker.begin() as session:
        result = await service.post(
            session, livemode=False, kind=kind, reference=new_id("pi"), lines=lines
        )
    model.apply(delta)
    model.history.append((result.transaction_id, delta))
