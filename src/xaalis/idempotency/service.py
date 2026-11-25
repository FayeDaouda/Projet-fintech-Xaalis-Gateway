"""Idempotence en base (ADR 0005).

Principe : la clé est **réservée dans la même transaction que l'effet** de la requête.

1. ``INSERT … ON CONFLICT DO UPDATE … WHERE expires_at <= now()`` : réserve une clé neuve,
   ou recycle une clé de plus de 24 h. Si la clé est déjà réservée par une transaction en
   cours, PostgreSQL fait **attendre** cette instruction jusqu'à son COMMIT ou ROLLBACK.
2. La réservation échoue (clé vivante) → on relit la réponse stockée et on la rejoue.
3. Sinon l'effet est exécuté, puis sa réponse (octets exacts) est enregistrée.

Ainsi, 20 requêtes simultanées avec la même clé ne produisent qu'un seul effet, et un
échec (ROLLBACK) libère la clé sans laisser d'état « en cours » à nettoyer.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import timedelta
from typing import Final

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from xaalis.core.errors import DomainError
from xaalis.idempotency.models import IdempotencyRecord

RETENTION: Final = timedelta(hours=24)


class IdempotencyKeyReusedError(DomainError):
    """Même clé, requête différente (corps, chemin ou méthode) → 422."""

    code = "idempotency_key_reused"


class IdempotencyKeyInProgressError(DomainError):  # pragma: no cover — défensif
    code = "idempotency_key_in_progress"


@dataclass(frozen=True, slots=True)
class StoredResponse:
    status: int
    body: bytes


def request_fingerprint(method: str, path: str, body: bytes) -> bytes:
    """Empreinte de la requête. Un corps JSON est canonisé (ordre des clés, espaces) :
    deux corps sémantiquement identiques sont « le même corps »."""
    try:
        canonical = json.dumps(
            json.loads(body), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
    except ValueError:
        canonical = body
    return hashlib.sha256(method.encode() + b" " + path.encode() + b"\n" + canonical).digest()


async def claim(
    session: AsyncSession, *, merchant_id: str, livemode: bool, key: str, fingerprint: bytes
) -> StoredResponse | None:
    """Réserve la clé ; renvoie ``None`` si l'appelant doit exécuter l'effet,
    sinon la réponse stockée à rejouer."""
    claimed = await session.scalar(
        pg_insert(IdempotencyRecord)
        .values(
            merchant_id=merchant_id,
            livemode=livemode,
            key=key,
            request_fingerprint=fingerprint,
            expires_at=func.now() + RETENTION,
        )
        .on_conflict_do_update(
            index_elements=["merchant_id", "livemode", "key"],
            set_={
                "request_fingerprint": fingerprint,
                "response_status": None,
                "response_body": None,
                "created_at": func.now(),
                "expires_at": func.now() + RETENTION,
            },
            where=IdempotencyRecord.expires_at <= func.now(),
        )
        .returning(IdempotencyRecord.key)
    )
    if claimed is not None:
        return None

    record = await session.scalar(
        select(IdempotencyRecord).where(
            IdempotencyRecord.merchant_id == merchant_id,
            IdempotencyRecord.livemode == livemode,
            IdempotencyRecord.key == key,
        )
    )
    if record is None or record.response_status is None or record.response_body is None:
        raise IdempotencyKeyInProgressError(
            "requête d'origine toujours en cours"
        )  # pragma: no cover
    if record.request_fingerprint != fingerprint:
        msg = "cette Idempotency-Key a déjà servi pour une requête différente"
        raise IdempotencyKeyReusedError(msg)
    return StoredResponse(status=record.response_status, body=record.response_body)


async def complete(
    session: AsyncSession, *, merchant_id: str, livemode: bool, key: str, status: int, body: bytes
) -> None:
    await session.execute(
        update(IdempotencyRecord)
        .where(
            IdempotencyRecord.merchant_id == merchant_id,
            IdempotencyRecord.livemode == livemode,
            IdempotencyRecord.key == key,
        )
        .values(response_status=status, response_body=body)
    )


async def purge_expired(session: AsyncSession) -> int:
    """Supprime les clés de plus de 24 h (tâche du worker)."""
    result = await session.execute(
        delete(IdempotencyRecord).where(IdempotencyRecord.expires_at <= func.now())
    )
    return int(getattr(result, "rowcount", 0) or 0)
