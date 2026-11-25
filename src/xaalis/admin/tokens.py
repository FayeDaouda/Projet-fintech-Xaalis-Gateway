"""Jetons JWT des administrateurs (HS256, durée de vie courte, rôle explicite).

Vérifications à chaque requête : signature, algorithme imposé (pas de ``alg: none``),
émetteur, audience, expiration, et présence du rôle ``admin``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final

import jwt

from xaalis.config import Settings

ADMIN_ROLE: Final = "admin"
_ALGORITHM: Final = "HS256"
_LEEWAY_SECONDS: Final = 5


class InvalidAdminTokenError(Exception):
    """Jeton absent, mal formé, expiré ou mal signé (→ 401)."""


class InsufficientRoleError(Exception):
    """Jeton valide mais sans le rôle administrateur (→ 403)."""


@dataclass(frozen=True, slots=True)
class AdminPrincipal:
    subject: str


def issue_admin_token(
    settings: Settings,
    subject: str,
    *,
    role: str = ADMIN_ROLE,
    now: datetime | None = None,
    ttl: timedelta | None = None,
) -> str:
    issued_at = now or datetime.now(UTC)
    lifetime = ttl or timedelta(seconds=settings.admin_token_ttl_seconds)
    claims = {
        "sub": subject,
        "role": role,
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
        "iat": issued_at,
        "exp": issued_at + lifetime,
    }
    return jwt.encode(claims, settings.jwt_secret.get_secret_value(), algorithm=_ALGORITHM)


def verify_admin_token(settings: Settings, token: str) -> AdminPrincipal:
    try:
        claims = jwt.decode(
            token,
            settings.jwt_secret.get_secret_value(),
            algorithms=[_ALGORITHM],
            audience=settings.jwt_audience,
            issuer=settings.jwt_issuer,
            leeway=_LEEWAY_SECONDS,
            options={"require": ["exp", "iat", "sub", "role"]},
        )
    except jwt.PyJWTError as exc:
        raise InvalidAdminTokenError(str(exc)) from exc
    if claims.get("role") != ADMIN_ROLE:
        raise InsufficientRoleError
    return AdminPrincipal(subject=str(claims["sub"]))
