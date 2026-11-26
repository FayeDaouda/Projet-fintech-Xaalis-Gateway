"""Hooks Schemathesis : se comporter comme un client correct de l'API Xaalis.

- **Authentification** par opération : JWT admin pour ``/v1/admin/*``, rien pour les callbacks
  opérateurs (signature propre), clé secrète du marchand de test ailleurs.
- **Idempotency-Key unique** par requête générée : réutiliser une clé avec un autre corps
  vaut 422 par spécification, ce n'est pas un défaut du contrat.
- **Signature HMAC** des requêtes sensibles (remboursement, virement), calculée sur les
  octets exacts envoyés.

Les jetons sont fournis par ``scripts/schemathesis.sh`` via l'environnement.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
import uuid
from typing import Any

import schemathesis

SIGNED_PATHS = frozenset({"/v1/refunds", "/v1/payouts"})


@schemathesis.auth()
class XaalisAuth:
    def get(self, case: Any, ctx: Any) -> dict[str, str]:
        return {
            "admin": os.environ["XAALIS_ST_ADMIN_TOKEN"],
            "merchant": os.environ["XAALIS_ST_SECRET_KEY"],
        }

    def set(self, case: Any, data: dict[str, str], ctx: Any) -> None:
        path: str = case.operation.path
        if path.startswith("/v1/operators/"):
            return
        token = data["admin"] if path.startswith("/v1/admin") else data["merchant"]
        case.headers = {**(case.headers or {}), "Authorization": f"Bearer {token}"}


@schemathesis.hook
def before_call(ctx: Any, case: Any, kwargs: dict[str, Any]) -> None:
    headers: dict[str, str] = dict(case.headers or {})
    if "Idempotency-Key" in headers:
        headers["Idempotency-Key"] = f"st-{uuid.uuid4()}"
    if case.operation.path in SIGNED_PATHS and case.operation.method.upper() == "POST":
        # Même sérialisation que ``requests`` (json=…) : la signature porte sur ces octets.
        try:
            raw = json.dumps(case.body, allow_nan=False).encode()
        except TypeError:  # corps absent (NotSet) ou non JSON : rien n'est envoyé
            raw = b""
        ts = int(time.time())
        secret = os.environ["XAALIS_ST_SECRET_KEY"].encode()
        headers["X-Xaalis-Timestamp"] = str(ts)
        headers["X-Xaalis-Signature"] = hmac.new(
            secret, str(ts).encode() + b"." + raw, hashlib.sha256
        ).hexdigest()
    case.headers = headers
