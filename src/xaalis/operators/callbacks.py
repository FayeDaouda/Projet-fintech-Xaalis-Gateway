"""Adaptateurs des callbacks entrants : un format par opérateur → un format interne unique.

=============  ===============================  ==========================================
Opérateur      Corps                            Signature
=============  ===============================  ==========================================
Go Pay         JSON                             ``X-GoPay-Signature`` = HMAC(corps brut)
Teranga Money  application/x-www-form-urlencoded champ ``signature`` = HMAC(txn|ref|amount|status)
Ndiaga Cash    JSON                             ``X-Ndiaga-Signature: t=<ts>,v1=<HMAC(ts.corps)>``
=============  ===============================  ==========================================

La signature est vérifiée **avant** toute interprétation du contenu, en temps constant.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final
from urllib.parse import parse_qs

from xaalis.core.errors import DomainError
from xaalis.core.operators import Operator
from xaalis.core.signatures import SignatureFailure, verify

NDIAGA_TOLERANCE_SECONDS: Final = 300


class CallbackOutcome(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class InvalidCallbackSignatureError(DomainError):
    code = "invalid_signature"


class MalformedCallbackError(DomainError):
    code = "malformed_callback"


@dataclass(frozen=True, slots=True)
class NormalizedCallback:
    operator: Operator
    operator_reference: str
    intent_id: str
    outcome: CallbackOutcome
    amount: int
    payload: dict[str, Any]


def _hmac_hex(secret: str, message: bytes) -> str:
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def _same(expected: str, received: str | None) -> bool:
    return received is not None and hmac.compare_digest(expected.encode(), received.encode())


def _int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int | str):
        msg = f"champ {field} invalide"
        raise MalformedCallbackError(msg)
    try:
        return int(value)
    except ValueError:
        msg = f"champ {field} invalide"
        raise MalformedCallbackError(msg) from None


def _json(body: bytes) -> dict[str, Any]:
    try:
        data = json.loads(body)
    except ValueError:
        raise MalformedCallbackError("corps JSON invalide") from None
    if not isinstance(data, dict):
        raise MalformedCallbackError("objet JSON attendu")
    return data


def _field(data: Mapping[str, Any], name: str) -> str:
    value = data.get(name)
    if not isinstance(value, str) or not value:
        msg = f"champ {name} manquant"
        raise MalformedCallbackError(msg)
    return value


def _parse_gopay(headers: Mapping[str, str], body: bytes, secret: str) -> NormalizedCallback:
    if not _same(_hmac_hex(secret, body), headers.get("x-gopay-signature")):
        raise InvalidCallbackSignatureError("signature Go Pay invalide")
    data = _json(body)
    status = _field(data, "status")
    return NormalizedCallback(
        operator=Operator.GOPAY,
        operator_reference=_field(data, "transaction_id"),
        intent_id=_field(data, "merchant_reference"),
        outcome=CallbackOutcome.SUCCEEDED if status == "SUCCESS" else CallbackOutcome.FAILED,
        amount=_int(data.get("amount"), "amount"),
        payload=data,
    )


def _parse_teranga(_: Mapping[str, str], body: bytes, secret: str) -> NormalizedCallback:
    form = {key: values[0] for key, values in parse_qs(body.decode(errors="replace")).items()}
    fields = [form.get(name, "") for name in ("txn_id", "reference", "amount", "status")]
    if not _same(_hmac_hex(secret, "|".join(fields).encode()), form.get("signature")):
        raise InvalidCallbackSignatureError("signature Teranga Money invalide")
    txn_id, reference, amount, status = fields
    if not txn_id or not reference:
        raise MalformedCallbackError("champs txn_id et reference requis")
    return NormalizedCallback(
        operator=Operator.TERANGA,
        operator_reference=txn_id,
        intent_id=reference,
        outcome=CallbackOutcome.SUCCEEDED if status == "OK" else CallbackOutcome.FAILED,
        amount=_int(amount, "amount"),
        payload={k: v for k, v in form.items() if k != "signature"},
    )


def _parse_ndiaga(headers: Mapping[str, str], body: bytes, secret: str) -> NormalizedCallback:
    parts = dict(
        item.split("=", 1)
        for item in headers.get("x-ndiaga-signature", "").split(",")
        if "=" in item
    )
    failure = verify(
        secret,
        timestamp=parts.get("t"),
        signature=parts.get("v1"),
        body=body,
        now=int(time.time()),
        tolerance=NDIAGA_TOLERANCE_SECONDS,
    )
    if failure is not None:
        detail = "horodatage trop ancien" if failure is SignatureFailure.STALE else "signature"
        msg = f"signature Ndiaga Cash invalide ({detail})"
        raise InvalidCallbackSignatureError(msg)
    data = _json(body)
    return NormalizedCallback(
        operator=Operator.NDIAGA,
        operator_reference=_field(data, "id"),
        intent_id=_field(data, "order"),
        outcome=CallbackOutcome.SUCCEEDED
        if data.get("state") == "accepted"
        else CallbackOutcome.FAILED,
        amount=_int(data.get("amount"), "amount"),
        payload=data,
    )


_PARSERS: Final = {
    Operator.GOPAY: _parse_gopay,
    Operator.TERANGA: _parse_teranga,
    Operator.NDIAGA: _parse_ndiaga,
}


def parse_callback(
    operator: Operator, headers: Mapping[str, str], body: bytes, secret: str
) -> NormalizedCallback:
    """Vérifie la signature puis normalise. ``headers`` : noms en minuscules."""
    return _PARSERS[operator](headers, body, secret)
