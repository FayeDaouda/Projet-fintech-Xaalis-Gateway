"""Identifiants publics : préfixe métier + ULID (triable dans le temps).

Exemple : ``pi_01JD8Q3W7Z4K2N6R5T9V0XBCYA``. Le préfixe rend un identifiant lisible
dans les logs et empêche de confondre un remboursement avec un paiement.
"""

from __future__ import annotations

from typing import Literal

from ulid import ULID

IdPrefix = Literal[
    "m",  # marchand
    "pi",  # payment intent
    "re",  # remboursement
    "po",  # virement (payout)
    "evt",  # événement webhook
    "we",  # endpoint webhook
    "ltx",  # transaction comptable
    "key",  # clé d'API
    "adm",  # administrateur
]


def new_id(prefix: IdPrefix) -> str:
    return f"{prefix}_{ULID()}"
