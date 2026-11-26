"""Routes des intents de paiement (lot 2)."""

from __future__ import annotations

import hashlib
from typing import Annotated, Literal

from fastapi import APIRouter, Header, Path, Query, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, StringConstraints

from xaalis.api.auth import AnyKeyDep, SecretKeyDep, enforce_rate_limit
from xaalis.api.idempotency import (
    IDEMPOTENCY_REQUIRED,
    IdempotencyKeyHeader,
    RequiredIdempotencyKey,
    run_idempotent,
)
from xaalis.api.pagination import DEFAULT_PAGE_SIZE, LimitQuery, Page, cursor_param
from xaalis.api.problems import Problem
from xaalis.api.resources import ResourcesDep, SessionDep
from xaalis.api.schemas import ApiModel, IntentAmount
from xaalis.core.phone import PHONE_PATTERN
from xaalis.merchants.service import MerchantPrincipal
from xaalis.payments import service as payments
from xaalis.payments.state_machine import IntentStatus
from xaalis.payments.views import PaymentIntentView
from xaalis.realtime.stream import parse_last_event_id, stream_intent

INTENT_ID_PATTERN = r"^pi_[0-9A-HJKMNP-TV-Z]{26}$"
MetadataKey = Annotated[str, StringConstraints(min_length=1, max_length=40)]
MetadataValue = Annotated[str, StringConstraints(max_length=500)]
IntentId = Annotated[
    str, Path(pattern=INTENT_ID_PATTERN, examples=["pi_01JD8Q3W7Z4K2N6R5T9V0XBCYA"])
]

router = APIRouter(
    prefix="/v1/payment-intents",
    tags=["payment-intents"],
    responses={
        401: {"model": Problem, "description": "Clé absente ou invalide."},
        429: {"model": Problem, "description": "Limite de débit atteinte (XG-13)."},
    },
)

_WRITE_ERRORS: dict[int | str, dict[str, object]] = {
    400: {"model": Problem, "description": "En-tête Idempotency-Key manquant."},
    403: {"model": Problem, "description": "Clé secrète requise."},
    404: {"model": Problem, "description": "Intent introuvable dans cet environnement."},
    409: {"model": Problem, "description": "Transition interdite ou limite du payeur."},
    422: {"model": Problem, "description": "Requête invalide ou Idempotency-Key réutilisée."},
}


class PaymentIntentCreate(ApiModel):
    # Entier JSON : 1000.5, "1000" et true sont refusés ; le domaine reçoit un int (XG-01).
    amount: IntentAmount = Field(examples=[12345])
    currency: Literal["XOF"] = Field(examples=["XOF"])  # XG-02 : toute autre devise → 422
    payer_phone: str = Field(pattern=PHONE_PATTERN, examples=["+221771234512"])  # XG-03
    description: str | None = Field(default=None, max_length=255)
    # Limites exprimées dans le schéma (propertyNames, additionalProperties, maxProperties).
    metadata: dict[MetadataKey, MetadataValue] = Field(default_factory=dict, max_length=20)


def _scope(principal: MerchantPrincipal) -> payments.Scope:
    return payments.Scope(merchant_id=principal.merchant_id, livemode=principal.livemode)


def _actor(principal: MerchantPrincipal) -> str:
    return f"merchant:{principal.key_id}"


@router.post(
    "",
    status_code=201,
    response_model=PaymentIntentView,
    responses=_WRITE_ERRORS,
    summary="Créer un intent de paiement",
    openapi_extra=IDEMPOTENCY_REQUIRED,
)
async def create_payment_intent(
    key: RequiredIdempotencyKey,
    body: PaymentIntentCreate,
    request: Request,
    principal: SecretKeyDep,
    session: SessionDep,
    resources: ResourcesDep,
) -> Response:
    # XG-13 : 20 créations par minute et par payeur ; le numéro est haché dans la clé Redis.
    payer_bucket = hashlib.sha256(body.payer_phone.encode()).hexdigest()[:24]
    await enforce_rate_limit(
        resources,
        bucket=f"payer:{payer_bucket}",
        limit=resources.settings.rate_limit_intents_per_payer_per_minute,
    )

    async def effect() -> tuple[int, BaseModel]:
        intent = await payments.create_intent(
            session,
            _scope(principal),
            amount=body.amount,
            currency=body.currency,
            payer_phone=body.payer_phone,
            metadata=body.metadata,
            description=body.description,
            actor=_actor(principal),
        )
        return 201, PaymentIntentView.of(intent)

    return await run_idempotent(session, request, principal, key, effect)


@router.post(
    "/{intent_id}/confirm",
    status_code=202,
    response_model=PaymentIntentView,
    responses=_WRITE_ERRORS,
    summary="Confirmer : lance le paiement chez l'opérateur (asynchrone)",
    openapi_extra=IDEMPOTENCY_REQUIRED,
)
async def confirm_payment_intent(
    key: RequiredIdempotencyKey,
    intent_id: IntentId,
    request: Request,
    principal: SecretKeyDep,
    session: SessionDep,
    resources: ResourcesDep,
) -> Response:
    # Disjoncteurs et taux de succès lus AVANT d'ouvrir la transaction (pas d'I/O sous verrou).
    snapshot = await resources.operator_health.snapshot()

    async def effect() -> tuple[int, BaseModel]:
        intent = await payments.confirm_intent(
            session, _scope(principal), intent_id, actor=_actor(principal), snapshot=snapshot
        )
        return 202, PaymentIntentView.of(intent)

    return await run_idempotent(session, request, principal, key, effect)


@router.post(
    "/{intent_id}/cancel",
    status_code=200,
    response_model=PaymentIntentView,
    responses=_WRITE_ERRORS,
    summary="Annuler un intent non confirmé",
)
async def cancel_payment_intent(
    intent_id: IntentId,
    request: Request,
    principal: SecretKeyDep,
    session: SessionDep,
    idempotency_key: IdempotencyKeyHeader = None,
) -> Response:
    async def effect() -> tuple[int, BaseModel]:
        intent = await payments.cancel_intent(
            session, _scope(principal), intent_id, actor=_actor(principal)
        )
        return 200, PaymentIntentView.of(intent)

    return await run_idempotent(session, request, principal, idempotency_key, effect)


@router.get(
    "/{intent_id}",
    response_model=PaymentIntentView,
    responses={404: {"model": Problem, "description": "Intent introuvable."}},
    summary="Consulter un intent (clé secrète ou publique)",
)
async def get_payment_intent(
    intent_id: IntentId, principal: AnyKeyDep, session: SessionDep
) -> PaymentIntentView:
    return PaymentIntentView.of(await payments.get_intent(session, _scope(principal), intent_id))


@router.get(
    "",
    response_model=Page[PaymentIntentView],
    responses={403: {"model": Problem, "description": "Clé secrète requise."}},
    summary="Lister les intents (pagination par curseur)",
)
async def list_payment_intents(
    principal: SecretKeyDep,
    session: SessionDep,
    status: Annotated[IntentStatus | None, Query()] = None,
    limit: LimitQuery = DEFAULT_PAGE_SIZE,
    starting_after: Annotated[str | None, cursor_param("pi")] = None,
) -> Page[PaymentIntentView]:
    rows, has_more = await payments.list_intents(
        session, _scope(principal), status=status, limit=limit, starting_after=starting_after
    )
    data = [PaymentIntentView.of(row) for row in rows]
    return Page[PaymentIntentView](
        data=data, has_more=has_more, next_cursor=data[-1].id if has_more else None
    )


@router.get(
    "/{intent_id}/stream",
    response_class=StreamingResponse,
    summary="Flux SSE des changements d'état (clé publique pk_)",
    responses={
        200: {"description": "Flux text/event-stream.", "content": {"text/event-stream": {}}},
        404: {"model": Problem, "description": "Intent introuvable."},
    },
)
async def stream_payment_intent(
    intent_id: IntentId,
    request: Request,
    principal: AnyKeyDep,
    resources: ResourcesDep,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID", max_length=32)] = None,
) -> StreamingResponse:
    async with resources.sessionmaker() as session:  # 404 AVANT d'ouvrir le flux (XG-12)
        await payments.get_intent(session, _scope(principal), intent_id)
    return StreamingResponse(
        stream_intent(
            intent_id=intent_id,
            sessionmaker=resources.sessionmaker,
            broker=request.app.state.broker,
            last_event_id=parse_last_event_id(last_event_id),
            is_disconnected=request.is_disconnected,
        ),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
