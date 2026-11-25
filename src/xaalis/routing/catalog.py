"""Catalogue des opérateurs : préfixes acceptés et coût supporté par Xaalis (XG-06).

Les coûts sont en points de base (1 pb = 0,01 %) : des entiers, pas de flottant (XG-01).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

from xaalis.core.errors import DomainError
from xaalis.core.operators import Operator
from xaalis.core.phone import national_prefix


@dataclass(frozen=True, slots=True)
class OperatorProfile:
    operator: Operator
    prefixes: frozenset[str]
    cost_bps: int


CATALOG: Final = MappingProxyType(
    {
        Operator.GOPAY: OperatorProfile(Operator.GOPAY, frozenset({"77", "78"}), cost_bps=90),
        Operator.TERANGA: OperatorProfile(Operator.TERANGA, frozenset({"76", "70"}), cost_bps=100),
        Operator.NDIAGA: OperatorProfile(Operator.NDIAGA, frozenset({"75", "77"}), cost_bps=80),
    }
)


class NoEligibleOperatorError(DomainError):
    code = "no_eligible_operator"


def eligible_operators(phone: str) -> list[Operator]:
    prefix = national_prefix(phone)
    return [profile.operator for profile in CATALOG.values() if prefix in profile.prefixes]


def rank_operators(
    phone: str, success_permille: Mapping[Operator, int] | None = None
) -> list[Operator]:
    """Opérateurs éligibles, du préféré au moins préféré.

    Critère 1 : le coût le plus faible. Critère 2, à coût égal : le meilleur taux de succès
    des 5 dernières minutes (en ‰, fourni par le lot 3). Critère 3 : ordre alphabétique,
    pour un résultat déterministe.
    """
    rates = success_permille or {}
    candidates = eligible_operators(phone)
    if not candidates:
        msg = f"aucun opérateur n'accepte le préfixe {national_prefix(phone)}"
        raise NoEligibleOperatorError(msg)
    return sorted(
        candidates,
        key=lambda op: (CATALOG[op].cost_bps, -rates.get(op, 1_000), op.value),
    )
