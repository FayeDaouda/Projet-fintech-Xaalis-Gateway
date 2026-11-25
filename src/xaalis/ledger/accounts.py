"""Plan de comptes : références de comptes et sens normal de leur solde."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from xaalis.core.operators import Operator


class AccountKind(StrEnum):
    OPERATOR_CLEARING = "operator_clearing"  # actif : ce que l'opérateur nous doit
    MERCHANT_PENDING = "merchant_pending"  # passif : dû au marchand, pas encore réglé
    MERCHANT_AVAILABLE = "merchant_available"  # passif : réglé, virable
    MERCHANT_PAYOUT = "merchant_payout"  # passif : déjà viré au marchand
    XAALIS_REVENUE = "xaalis_revenue"  # produit : commissions

    @property
    def normal_sign(self) -> int:
        """+1 pour un compte à solde débiteur, −1 pour un compte à solde créditeur."""
        return 1 if self is AccountKind.OPERATOR_CLEARING else -1


_MERCHANT_KINDS = frozenset(
    {AccountKind.MERCHANT_PENDING, AccountKind.MERCHANT_AVAILABLE, AccountKind.MERCHANT_PAYOUT}
)


@dataclass(frozen=True, slots=True)
class AccountRef:
    """Identité métier d'un compte, indépendante de son identifiant en base."""

    kind: AccountKind
    merchant_id: str | None = None
    operator: Operator | None = None

    def __post_init__(self) -> None:
        if (self.kind in _MERCHANT_KINDS) != (self.merchant_id is not None):
            msg = f"le compte {self.kind} exige un marchand ssi c'est un compte marchand"
            raise ValueError(msg)
        if (self.kind is AccountKind.OPERATOR_CLEARING) != (self.operator is not None):
            msg = f"le compte {self.kind} exige un opérateur ssi c'est un compte de compensation"
            raise ValueError(msg)

    @property
    def code(self) -> str:
        qualifier = self.merchant_id or self.operator
        return f"{self.kind}:{qualifier}" if qualifier else str(self.kind)

    # Constructeurs nommés : le code appelant se lit comme le plan de comptes.
    @classmethod
    def operator_clearing(cls, operator: Operator) -> AccountRef:
        return cls(AccountKind.OPERATOR_CLEARING, operator=operator)

    @classmethod
    def merchant_pending(cls, merchant_id: str) -> AccountRef:
        return cls(AccountKind.MERCHANT_PENDING, merchant_id=merchant_id)

    @classmethod
    def merchant_available(cls, merchant_id: str) -> AccountRef:
        return cls(AccountKind.MERCHANT_AVAILABLE, merchant_id=merchant_id)

    @classmethod
    def merchant_payout(cls, merchant_id: str) -> AccountRef:
        return cls(AccountKind.MERCHANT_PAYOUT, merchant_id=merchant_id)

    @classmethod
    def revenue(cls) -> AccountRef:
        return cls(AccountKind.XAALIS_REVENUE)
