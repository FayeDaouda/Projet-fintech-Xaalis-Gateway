"""Remboursements et soldes du marchand (XG-08 à XG-11)."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, Field

from xaalis.api.auth import SecretKeyDep, SignedRequestDep
from xaalis.api.idempotency import (
    IDEMPOTENCY_REQUIRED,
    RequiredIdempotencyKey,
    run_idempotent,
)
from xaalis.api.problems import Problem
from xaalis.api.resources import SessionDep
from xaalis.api.schemas import ApiModel, RefundAmount
from xaalis.ledger.service import merchant_balances
from xaalis.payments.service import Scope
from xaalis.refunds.service import create_refund
from xaalis.refunds.views import RefundView

router = APIRouter(tags=["refunds"])


class RefundCreate(ApiModel):
    payment_intent: str = Field(pattern=r"^pi_[0-9A-HJKMNP-TV-Z]{26}$")
    amount: RefundAmount | None = Field(
        default=None, description="Montant à rembourser ; par défaut, tout le reste remboursable."
    )
    reason: str | None = Field(default=None, max_length=255)


@router.post(
    "/v1/refunds",
    status_code=201,
    response_model=RefundView,
    summary="Rembourser tout ou partie d'un paiement (requête signée)",
    openapi_extra=IDEMPOTENCY_REQUIRED,
    responses={
        400: {"model": Problem, "description": "Idempotency-Key manquante."},
        401: {"model": Problem, "description": "Clé ou signature HMAC invalide."},
        404: {"model": Problem, "description": "Intent introuvable."},
        409: {
            "model": Problem,
            "description": "État non remboursable, délai de 30 jours dépassé, montant "
            "remboursable dépassé ou solde insuffisant.",
        },
        422: {"model": Problem, "description": "Requête invalide."},
    },
)
async def refund(
    key: RequiredIdempotencyKey,
    body: RefundCreate,
    request: Request,
    principal: SignedRequestDep,
    session: SessionDep,
) -> Response:

    async def effect() -> tuple[int, BaseModel]:
        created = await create_refund(
            session,
            Scope(principal.merchant_id, principal.livemode),
            body.payment_intent,
            amount=body.amount,
            reason=body.reason,
            actor=f"merchant:{principal.key_id}",
        )
        return 201, RefundView.of(created)

    return await run_idempotent(session, request, principal, key, effect)


class Balance(ApiModel):
    object: Literal["balance"] = "balance"
    livemode: bool
    currency: Literal["XOF"] = "XOF"
    pending: int = Field(description="Fonds encaissés, pas encore réglés (T+1).")
    available: int = Field(description="Fonds réglés, virables.")
    paid_out: int = Field(description="Total déjà viré au marchand.")


@router.get("/v1/balance", response_model=Balance, summary="Soldes du marchand")
async def balance(principal: SecretKeyDep, session: SessionDep) -> Balance:
    balances = await merchant_balances(session, principal.merchant_id, livemode=principal.livemode)
    return Balance(
        livemode=principal.livemode,
        pending=balances.pending,
        available=balances.available,
        paid_out=balances.paid_out,
    )
