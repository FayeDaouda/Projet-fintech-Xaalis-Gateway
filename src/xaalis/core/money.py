"""Montants en francs CFA (XOF).

Le XOF n'a pas de subdivision : un montant est un ``int`` Python, stocké en ``BIGINT``.
Aucun ``float`` n'apparaît dans le domaine (XG-01) ; un test d'architecture le vérifie.
"""

from __future__ import annotations

from typing import Final, Literal

Currency = Literal["XOF"]

CURRENCY: Final[Currency] = "XOF"  # XG-02 : devise unique
MIN_INTENT_AMOUNT: Final = 100  # XG-01
MAX_INTENT_AMOUNT: Final = 2_000_000  # XG-01


class InvalidAmountError(ValueError):
    """Montant non entier, ou hors des bornes d'un intent."""


def ensure_integer_amount(amount: object) -> int:
    # bool est un sous-type d'int : True ne doit pas passer pour 1 FCFA.
    if not isinstance(amount, int) or isinstance(amount, bool):
        msg = f"un montant doit être un entier en XOF, reçu {type(amount).__name__}"
        raise InvalidAmountError(msg)
    return amount


def ensure_intent_amount(amount: object) -> int:
    value = ensure_integer_amount(amount)
    if not MIN_INTENT_AMOUNT <= value <= MAX_INTENT_AMOUNT:
        msg = f"le montant doit être compris entre {MIN_INTENT_AMOUNT} et {MAX_INTENT_AMOUNT} XOF"
        raise InvalidAmountError(msg)
    return value
