"""Signatures HMAC horodatées, communes aux requêtes entrantes et aux webhooks sortants.

Message signé : ``f"{timestamp}.{corps brut}"`` ; signature : HMAC-SHA-256 en hexadécimal.
Le corps est signé **tel qu'il a été transmis**, octet par octet : on ne re-sérialise
jamais le JSON avant de vérifier (l'ordre des clés ou les espaces changeraient l'empreinte).
"""

from __future__ import annotations

import hashlib
import hmac
from enum import StrEnum
from typing import Final

TOLERANCE_SECONDS: Final = 300


class SignatureFailure(StrEnum):
    MALFORMED = "malformed"
    STALE = "stale"
    MISMATCH = "mismatch"


def sign(secret: str, timestamp: int, body: bytes) -> str:
    message = str(timestamp).encode() + b"." + body
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def verify(
    secret: str,
    *,
    timestamp: str | None,
    signature: str | None,
    body: bytes,
    now: int,
    tolerance: int = TOLERANCE_SECONDS,
) -> SignatureFailure | None:
    """Renvoie ``None`` si la signature est valide, sinon la cause de l'échec."""
    if not timestamp or not signature or not timestamp.isdigit() or len(timestamp) > 12:
        return SignatureFailure.MALFORMED
    ts = int(timestamp)
    # Tolérance dans les deux sens : horloge du client en avance ou en retard.
    if abs(now - ts) > tolerance:
        return SignatureFailure.STALE
    candidate = signature.removeprefix("v1=")
    expected = sign(secret, ts, body)
    if not hmac.compare_digest(candidate.encode(), expected.encode()):
        return SignatureFailure.MISMATCH
    return None
