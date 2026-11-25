"""Clés d'API : format, génération, empreinte (voir ADR 0004).

Format : ``{pk|sk}_{test|live}_{32 caractères base62}``, soit environ 190 bits
d'entropie. Seule l'empreinte ``HMAC-SHA-256(pepper, clé)`` est stockée ; la clé
en clair n'est montrée qu'une fois, à sa création.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import string
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

_ALPHABET: Final = string.ascii_letters + string.digits  # base62
_RANDOM_LENGTH: Final = 32
_KEY_PATTERN: Final = re.compile(r"^(pk|sk)_(test|live)_([A-Za-z0-9]{32})$")


class KeyKind(StrEnum):
    PUBLISHABLE = "publishable"
    SECRET = "secret"  # noqa: S105 — catégorie de clé, pas un secret

    @property
    def prefix(self) -> str:
        return "pk" if self is KeyKind.PUBLISHABLE else "sk"


@dataclass(frozen=True, slots=True)
class ParsedKey:
    kind: KeyKind
    livemode: bool


def generate_key(kind: KeyKind, *, livemode: bool) -> str:
    body = "".join(secrets.choice(_ALPHABET) for _ in range(_RANDOM_LENGTH))
    return f"{kind.prefix}_{'live' if livemode else 'test'}_{body}"


def parse_key(raw: str) -> ParsedKey | None:
    """Analyse la forme d'une clé ; ``None`` si elle ne peut pas être une clé Xaalis."""
    match = _KEY_PATTERN.fullmatch(raw)
    if match is None:
        return None
    prefix, mode, _ = match.groups()
    kind = KeyKind.PUBLISHABLE if prefix == "pk" else KeyKind.SECRET
    return ParsedKey(kind=kind, livemode=mode == "live")


def fingerprint(pepper: str, raw_key: str) -> bytes:
    """Empreinte stockée en base : HMAC-SHA-256 keyed par le pepper (32 octets)."""
    return hmac.new(pepper.encode(), raw_key.encode(), hashlib.sha256).digest()


def redact(raw_key: str) -> str:
    """Forme affichable sans risque : ``sk_test_…Ab12``."""
    head, _, tail = raw_key.rpartition("_")
    return f"{head}_…{tail[-4:]}"
