"""Base commune des schémas d'API : aucun champ inconnu accepté ni divulgué (OWASP API3).

Types d'entrée alignés sur JSON Schema (ce que valide Schemathesis) :

- ``JsonInteger`` : un nombre JSON entier. ``1420`` et ``1420.0`` sont des entiers au sens
  de JSON Schema et sont convertis exactement en ``int`` ; ``1420.5``, ``"1420"`` et ``true``
  sont refusés. Le domaine ne reçoit donc jamais de flottant (XG-01).
- ``IsoDate`` : une date ``AAAA-MM-JJ`` en chaîne ; un nombre (que Pydantic interpréterait
  comme un horodatage Unix) est refusé.
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Any, Final

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator

from xaalis.core.money import MAX_INTENT_AMOUNT, MIN_INTENT_AMOUNT

_REPLACEMENT: Final = "\ufffd"


def _is_unstorable(char: str) -> bool:
    # U+0000 et les demi-paires UTF-16 isolées (U+D800–U+DFFF) sont des chaînes JSON valides,
    # mais ne sont représentables ni en UTF-8 ni dans un champ texte PostgreSQL.
    return char == "\x00" or "\ud800" <= char <= "\udfff"


def normalize_text(value: str) -> str:
    if not any(_is_unstorable(char) for char in value):
        return value
    return "".join(_REPLACEMENT if _is_unstorable(char) else char for char in value)


def _normalize_tree(value: Any) -> Any:
    if isinstance(value, str):
        return normalize_text(value)
    if isinstance(value, dict):
        return {_normalize_tree(key): _normalize_tree(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_normalize_tree(item) for item in value]
    return value


class ApiModel(BaseModel):
    """Base des modèles d'API : champs inconnus refusés, texte rendu stockable.

    Toute chaîne d'entrée (valeurs et clés, à toute profondeur) voit ses caractères non
    stockables remplacés par U+FFFD, sans changer sa longueur : un nom contenant « \\u0000 »
    ne peut plus provoquer d'erreur 500 à l'écriture en base.
    """

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def _storable_text(cls, data: Any) -> Any:
        return _normalize_tree(data)


def _json_number_only(value: object) -> object:
    if isinstance(value, bool | str | bytes):
        msg = "un nombre entier JSON est attendu"
        raise ValueError(msg)
    return value


def _iso_string_only(value: object) -> object:
    if not isinstance(value, str | dt.date):
        msg = "une date AAAA-MM-JJ est attendue"
        raise ValueError(msg)
    return value


JsonInteger = Annotated[int, BeforeValidator(_json_number_only)]

# Montants : les bornes sont déclarées AVANT le validateur, sinon Pydantic ne peut pas les
# rattacher à l'entier et publie des clés « ge/le » qui ne sont pas du JSON Schema.
IntentAmount = Annotated[
    int, Field(ge=MIN_INTENT_AMOUNT, le=MAX_INTENT_AMOUNT), BeforeValidator(_json_number_only)
]
RefundAmount = Annotated[int, Field(ge=1, le=MAX_INTENT_AMOUNT), BeforeValidator(_json_number_only)]
IsoDate = Annotated[dt.date, BeforeValidator(_iso_string_only)]
