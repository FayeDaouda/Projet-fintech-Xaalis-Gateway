"""Numéro du payeur (XG-03) : E.164 sénégalais, ``+221`` suivi de 9 chiffres commençant par 7.

Le numéro complet n'est utilisé que pour router le paiement vers l'opérateur ; il est
**masqué** dans toutes les réponses et tous les logs : ``+221 77 *** ** 12``.
"""

from __future__ import annotations

import re
from typing import Final

from xaalis.core.errors import DomainError

PHONE_PATTERN: Final = r"^\+2217[0-9]{8}$"
_PHONE_RE: Final = re.compile(PHONE_PATTERN)
# Variante tolérante pour repérer un numéro dans du texte libre (logs), espaces compris.
_PHONE_IN_TEXT_RE: Final = re.compile(r"\+221\s?(7\d)\s?\d{3}\s?\d{2}\s?(\d{2})")


class InvalidPhoneError(DomainError):
    code = "invalid_phone"


def normalize_phone(raw: str) -> str:
    if not _PHONE_RE.fullmatch(raw):
        msg = "le numéro doit être au format +2217XXXXXXXX (E.164 sénégalais)"
        raise InvalidPhoneError(msg)
    return raw


def national_prefix(phone: str) -> str:
    """Les deux chiffres qui identifient le réseau : ``+22177…`` → ``"77"``."""
    return normalize_phone(phone)[4:6]


def mask_phone(phone: str) -> str:
    digits = normalize_phone(phone)[4:]
    return f"+221 {digits[:2]} *** ** {digits[-2:]}"


def mask_phones_in_text(text: str) -> str:
    return _PHONE_IN_TEXT_RE.sub(lambda m: f"+221 {m.group(1)} *** ** {m.group(2)}", text)
