"""Format des callbacks de chaque opérateur, tel qu'un vrai partenaire les publierait.

Volontairement indépendant du code de la passerelle : si les deux divergent, les tests
de bout en bout échouent, comme avec un vrai opérateur.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlencode


@dataclass(frozen=True, slots=True)
class CallbackRequest:
    body: bytes
    headers: dict[str, str]


def _sign(secret: str, message: bytes) -> str:
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def gopay(
    secret: str, *, reference: str, operator_reference: str, amount: int, ok: bool
) -> CallbackRequest:
    body = json.dumps(
        {
            "event": "payment.completed" if ok else "payment.failed",
            "transaction_id": operator_reference,
            "merchant_reference": reference,
            "amount": amount,
            "currency": "XOF",
            "status": "SUCCESS" if ok else "FAILED",
            "timestamp": datetime.now(UTC).isoformat(),
        }
    ).encode()
    return CallbackRequest(
        body, {"Content-Type": "application/json", "X-GoPay-Signature": _sign(secret, body)}
    )


def teranga(
    secret: str, *, reference: str, operator_reference: str, amount: int, ok: bool
) -> CallbackRequest:
    status = "OK" if ok else "KO"
    signature = _sign(secret, f"{operator_reference}|{reference}|{amount}|{status}".encode())
    body = urlencode(
        {
            "txn_id": operator_reference,
            "reference": reference,
            "amount": str(amount),
            "status": status,
            "signature": signature,
        }
    ).encode()
    return CallbackRequest(body, {"Content-Type": "application/x-www-form-urlencoded"})


def ndiaga(
    secret: str, *, reference: str, operator_reference: str, amount: int, ok: bool
) -> CallbackRequest:
    body = json.dumps(
        {
            "id": operator_reference,
            "order": reference,
            "amount": amount,
            "state": "accepted" if ok else "rejected",
        }
    ).encode()
    ts = int(time.time())
    signature = _sign(secret, f"{ts}.".encode() + body)
    return CallbackRequest(
        body, {"Content-Type": "application/json", "X-Ndiaga-Signature": f"t={ts},v1={signature}"}
    )


BUILDERS = {"gopay": gopay, "teranga": teranga, "ndiaga": ndiaga}
