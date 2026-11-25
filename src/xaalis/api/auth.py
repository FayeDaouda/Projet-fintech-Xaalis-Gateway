"""Dépendances d'authentification de l'API.

- **Administrateurs** : JWT ``Authorization: Bearer <jwt>``, rôle ``admin``.
- **Marchands** : clé d'API ``Authorization: Bearer sk_…`` (ou ``pk_…`` en lecture).
- **Endpoints sensibles** (remboursement, virement) : clé secrète **et** signature HMAC
  du corps, en-têtes ``X-Xaalis-Timestamp`` et ``X-Xaalis-Signature`` (tolérance 300 s).

Les schémas de sécurité sont déclarés dans l'OpenAPI : Schemathesis et la documentation
interactive savent quelles routes exigent quel jeton.
"""

from __future__ import annotations

import time
from typing import Annotated

import structlog
from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from xaalis.admin.tokens import (
    AdminPrincipal,
    InsufficientRoleError,
    InvalidAdminTokenError,
    verify_admin_token,
)
from xaalis.api.problems import ProblemError
from xaalis.api.resources import Resources, ResourcesDep
from xaalis.core.signatures import SignatureFailure, verify
from xaalis.merchants.keys import KeyKind
from xaalis.merchants.service import MerchantPrincipal, authenticate_key

TIMESTAMP_HEADER = "X-Xaalis-Timestamp"
SIGNATURE_HEADER = "X-Xaalis-Signature"

_admin_bearer = HTTPBearer(
    scheme_name="AdminJWT", description="JWT administrateur (rôle `admin`).", auto_error=False
)
_api_key_bearer = HTTPBearer(
    scheme_name="ApiKey",
    description="Clé d'API du marchand : `sk_test_…` / `sk_live_…` (ou `pk_…` en lecture).",
    auto_error=False,
)
_ADMIN_CHALLENGE = {"WWW-Authenticate": 'Bearer realm="xaalis-admin"'}
_API_CHALLENGE = {"WWW-Authenticate": 'Bearer realm="xaalis-api"'}

BearerCredentials = HTTPAuthorizationCredentials | None


# --------------------------------------------------------------------------- admin


async def require_admin(
    resources: ResourcesDep,
    credentials: Annotated[BearerCredentials, Depends(_admin_bearer)],
) -> AdminPrincipal:
    if credentials is None:
        raise ProblemError(
            401, "unauthenticated", "Jeton administrateur requis.", headers=_ADMIN_CHALLENGE
        )
    try:
        principal = verify_admin_token(resources.settings, credentials.credentials)
    except InvalidAdminTokenError:
        raise ProblemError(
            401,
            "invalid_token",
            "Jeton administrateur invalide ou expiré.",
            headers=_ADMIN_CHALLENGE,
        ) from None
    except InsufficientRoleError:
        raise ProblemError(403, "forbidden", "Rôle administrateur requis.") from None
    structlog.contextvars.bind_contextvars(admin=principal.subject)
    return principal


AdminDep = Annotated[AdminPrincipal, Depends(require_admin)]


# --------------------------------------------------------------------------- marchands


async def _authenticate_merchant(
    resources: ResourcesDep, credentials: BearerCredentials
) -> MerchantPrincipal:
    if credentials is None:
        raise ProblemError(401, "unauthenticated", "Clé d'API requise.", headers=_API_CHALLENGE)
    # Session dédiée et courte : la route garde la maîtrise de sa propre transaction.
    async with resources.sessionmaker() as session:
        principal = await authenticate_key(
            session,
            credentials.credentials,
            pepper=resources.settings.api_key_pepper.get_secret_value(),
        )
    if principal is None:
        raise ProblemError(
            401,
            "invalid_api_key",
            "Clé d'API invalide, expirée ou révoquée.",
            headers=_API_CHALLENGE,
        )
    structlog.contextvars.bind_contextvars(
        merchant_id=principal.merchant_id, livemode=principal.livemode
    )
    await enforce_rate_limit(
        resources,
        bucket=f"key:{principal.key_id}",
        limit=resources.settings.rate_limit_per_key_per_minute,
    )
    return principal


async def enforce_rate_limit(resources: Resources, *, bucket: str, limit: int) -> None:
    """XG-13 : 429 avec ``Retry-After`` au-delà de ``limit`` requêtes par minute."""
    if not resources.settings.rate_limit_enabled:
        return
    decision = await resources.rate_limiter.hit(bucket, limit=limit, period_seconds=60)
    if not decision.allowed:
        raise ProblemError(
            429,
            "rate_limited",
            f"Limite de {limit} requêtes par minute atteinte.",
            headers={"Retry-After": str(decision.retry_after_seconds)},
        )


async def require_secret_key(
    resources: ResourcesDep,
    credentials: Annotated[BearerCredentials, Depends(_api_key_bearer)],
) -> MerchantPrincipal:
    principal = await _authenticate_merchant(resources, credentials)
    if principal.kind is not KeyKind.SECRET:
        raise ProblemError(
            403, "secret_key_required", "Cette opération exige une clé secrète (sk_…)."
        )
    return principal


async def require_any_key(
    resources: ResourcesDep,
    credentials: Annotated[BearerCredentials, Depends(_api_key_bearer)],
) -> MerchantPrincipal:
    return await _authenticate_merchant(resources, credentials)


SecretKeyDep = Annotated[MerchantPrincipal, Depends(require_secret_key)]
AnyKeyDep = Annotated[MerchantPrincipal, Depends(require_any_key)]


# --------------------------------------------------------------------------- signature

_SIGNATURE_PROBLEMS = {
    SignatureFailure.MALFORMED: (
        "signature_required",
        f"En-têtes {TIMESTAMP_HEADER} et {SIGNATURE_HEADER} requis et bien formés.",
    ),
    SignatureFailure.STALE: (
        "signature_expired",
        "Horodatage hors de la tolérance de 300 secondes.",
    ),
    SignatureFailure.MISMATCH: ("invalid_signature", "La signature de la requête est invalide."),
}


async def require_signed_request(
    request: Request,
    principal: SecretKeyDep,
    credentials: Annotated[BearerCredentials, Depends(_api_key_bearer)],
) -> MerchantPrincipal:
    """Clé secrète + signature ``HMAC-SHA-256(sk, f"{timestamp}.{corps}")``.

    Le serveur ne stocke que l'empreinte de la clé ; il signe avec la clé présentée dans
    ``Authorization``, déjà authentifiée par ``require_secret_key``. La signature prouve
    l'intégrité du corps et borne la fenêtre de rejeu à 300 s ; au-delà, l'``Idempotency-Key``
    obligatoire sur ces routes garantit qu'un rejeu ne produit pas de second effet.
    """
    assert credentials is not None  # noqa: S101 — garanti par require_secret_key
    failure = verify(
        credentials.credentials,
        timestamp=request.headers.get(TIMESTAMP_HEADER),
        signature=request.headers.get(SIGNATURE_HEADER),
        body=await request.body(),
        now=int(time.time()),
    )
    if failure is not None:
        code, detail = _SIGNATURE_PROBLEMS[failure]
        raise ProblemError(401, code, detail)
    return principal


SignedRequestDep = Annotated[MerchantPrincipal, Depends(require_signed_request)]
