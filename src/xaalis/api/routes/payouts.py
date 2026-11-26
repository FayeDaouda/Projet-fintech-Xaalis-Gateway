"""Virements vers le marchand (lot 6)."""

from __future__ import annotations

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel

from xaalis.api.auth import SignedRequestDep
from xaalis.api.idempotency import IdempotencyKeyHeader, require_key, run_idempotent
from xaalis.api.problems import Problem
from xaalis.api.resources import SessionDep
from xaalis.payments.service import Scope
from xaalis.settlement.service import create_payout
from xaalis.settlement.views import PayoutView

router = APIRouter(tags=["payouts"])


@router.post(
    "/v1/payouts",
    status_code=201,
    response_model=PayoutView,
    summary="Virer tout le solde disponible (requête signée)",
    responses={
        400: {"model": Problem, "description": "Idempotency-Key manquante."},
        401: {"model": Problem, "description": "Clé ou signature HMAC invalide."},
        409: {"model": Problem, "description": "Aucun fonds disponible."},
    },
)
async def payout(
    request: Request,
    principal: SignedRequestDep,
    session: SessionDep,
    idempotency_key: IdempotencyKeyHeader = None,
) -> Response:
    key = require_key(idempotency_key)

    async def effect() -> tuple[int, BaseModel]:
        created = await create_payout(
            session,
            Scope(principal.merchant_id, principal.livemode),
            actor=f"merchant:{principal.key_id}",
        )
        return 201, PayoutView.of(created)

    return await run_idempotent(session, request, principal, key, effect)
