"""Lignes d'écriture et leur validation (pure, sans base de données).

Convention de signe (ADR 0001) : débit = montant positif, crédit = montant négatif.
On ne construit jamais une ligne avec un signe « à la main » : on passe par
``debit()`` / ``credit()`` qui n'acceptent que des montants strictement positifs.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from xaalis.core.money import ensure_integer_amount
from xaalis.ledger.accounts import AccountRef


class TransactionKind(StrEnum):
    PAYMENT_CAPTURE = "payment_capture"
    SETTLEMENT = "settlement"
    REFUND = "refund"
    PAYOUT = "payout"
    REVERSAL = "reversal"


class UnbalancedPostingError(ValueError):
    """Les lignes proposées ne forment pas une transaction comptable valide."""


@dataclass(frozen=True, slots=True)
class Line:
    account: AccountRef
    amount: int  # signé

    def negated(self) -> Line:
        return Line(self.account, -self.amount)


def _magnitude(amount: int) -> int:
    value = ensure_integer_amount(amount)
    if value <= 0:
        msg = f"un débit ou un crédit porte un montant strictement positif, reçu {value}"
        raise UnbalancedPostingError(msg)
    return value


def debit(account: AccountRef, amount: int) -> Line:
    return Line(account, _magnitude(amount))


def credit(account: AccountRef, amount: int) -> Line:
    return Line(account, -_magnitude(amount))


def validate_lines(lines: Sequence[Line]) -> None:
    """Vérifie l'invariant XG-16 avant d'écrire : ≥ 2 lignes, non nulles, somme nulle."""
    if len(lines) < 2:
        msg = "une transaction comptable compte au moins deux écritures"
        raise UnbalancedPostingError(msg)
    for line in lines:
        if ensure_integer_amount(line.amount) == 0:
            msg = f"écriture nulle sur {line.account.code}"
            raise UnbalancedPostingError(msg)
    total = sum(line.amount for line in lines)
    if total != 0:
        msg = f"transaction déséquilibrée : la somme des écritures vaut {total}"
        raise UnbalancedPostingError(msg)
