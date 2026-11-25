"""Commission Xaalis (XG-04, XG-05).

Commission = 1,8 % du montant, arrondie à l'unité **supérieure**, bornée à
[100 ; 5 000] XOF. Le calcul est fait en arithmétique entière : ``ceil(a × 18 / 1000)``
s'écrit ``-(-a × 18 // 1000)``, sans passer par un flottant ni par ``Decimal``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from xaalis.core.money import ensure_intent_amount

FEE_RATE_NUMERATOR: Final = 18  # XG-04 : 1,8 % = 18 / 1000
FEE_RATE_DENOMINATOR: Final = 1_000
FEE_MIN: Final = 100  # XG-05
FEE_MAX: Final = 5_000  # XG-05


def _ceil_div(numerator: int, denominator: int) -> int:
    return -(-numerator // denominator)


def compute_fee(amount: int) -> int:
    value = ensure_intent_amount(amount)
    proportional = _ceil_div(value * FEE_RATE_NUMERATOR, FEE_RATE_DENOMINATOR)
    return min(max(proportional, FEE_MIN), FEE_MAX)


@dataclass(frozen=True, slots=True)
class FeeBreakdown:
    amount: int
    fee: int
    net: int


def fee_breakdown(amount: int) -> FeeBreakdown:
    """Montant, commission et net marchand (``net = amount − fee``, jamais négatif)."""
    fee = compute_fee(amount)
    return FeeBreakdown(amount=amount, fee=fee, net=amount - fee)
