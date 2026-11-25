"""Exécution idempotente d'une route POST (ADR 0005).

La route fournit l'effet sous forme de coroutine ; ce module gère la transaction,
la réservation de la clé, le rejeu octet pour octet et l'en-tête ``Idempotent-Replayed``.
Les réponses contenant un secret (création de marchand, rotation de clé) ne passent
**pas** par ici : on ne stocke jamais un secret en clair dans ``idempotency_keys``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Annotated

from fastapi import Header, Request, Response
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from xaalis.api.problems import ProblemError
from xaalis.idempotency import service as idempotency
from xaalis.merchants.service import MerchantPrincipal

IDEMPOTENCY_HEADER = "Idempotency-Key"
REPLAYED_HEADER = "Idempotent-Replayed"
JSON = "application/json"

IdempotencyKeyHeader = Annotated[
    str | None,
    Header(
        alias=IDEMPOTENCY_HEADER,
        min_length=1,
        max_length=255,
        description="Clé unique choisie par le client ; conservée 24 h.",
    ),
]

Effect = Callable[[], Awaitable[tuple[int, BaseModel]]]


def require_key(key: str | None) -> str:
    if key is None:
        raise ProblemError(
            400, "idempotency_key_required", f"L'en-tête {IDEMPOTENCY_HEADER} est obligatoire."
        )
    return key


async def run_idempotent(
    session: AsyncSession,
    request: Request,
    principal: MerchantPrincipal,
    key: str | None,
    effect: Effect,
) -> Response:
    async with session.begin():
        if key is None:
            status, model = await effect()
            body = model.model_dump_json().encode()
        else:
            fingerprint = idempotency.request_fingerprint(
                request.method, request.url.path, await request.body()
            )
            stored = await idempotency.claim(
                session,
                merchant_id=principal.merchant_id,
                livemode=principal.livemode,
                key=key,
                fingerprint=fingerprint,
            )
            if stored is not None:
                return Response(
                    content=stored.body,
                    status_code=stored.status,
                    media_type=JSON,
                    headers={REPLAYED_HEADER: "true"},
                )
            status, model = await effect()
            body = model.model_dump_json().encode()
            await idempotency.complete(
                session,
                merchant_id=principal.merchant_id,
                livemode=principal.livemode,
                key=key,
                status=status,
                body=body,
            )
    return Response(content=body, status_code=status, media_type=JSON)
