"""Routes des intents de paiement (lot 2)."""

from __future__ import annotations

import hashlib
from typing import Annotated, Literal

from fastapi import APIRouter, Path, Query, Request, Response
from pydantic import BaseModel, Field, StrictInt, field_validator

from xaalis.api.auth import AnyKeyDep, SecretKeyDep, enforce_rate_limit
from xaalis.api.idempotency import IdempotencyKeyHeader, require_key, run_idempotent
from xaalis.api.pagination import DEFAULT_PAGE_SIZE, LimitQuery, Page, cursor_param
from xaalis.api.problems import Problem
from xaalis.api.resources import ResourcesDep, SessionDep
from xaalis.api.schemas import ApiModel
from xaalis.core.money import MAX_INTENT_AMOUNT, MIN_INTENT_AMOUNT
from xaalis.core.phone import PHONE_PATTERN
from xaalis.merchants.service import MerchantPrincipal
from xaalis.payments import service as payments
from xaalis.payments.state_machine import IntentStatus
from xaalis.payments.views import PaymentIntentView

INTENT_ID_PATTERN = r"^pi_[0-9A-HJKMNP-TV-Z]{26}$"
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
    # StrictInt : "1000", 1000.0 et true sont refusés — pas de float, ni de conversion (XG-01).
    amount: StrictInt = Field(ge=MIN_INTENT_AMOUNT, le=MAX_INTENT_AMOUNT, examples=[12345])
    currency: Literal["XOF"] = Field(examples=["XOF"])  # XG-02 : toute autre devise → 422
    payer_phone: str = Field(pattern=PHONE_PATTERN, examples=["+221771234512"])  # XG-03
    description: str | None = Field(default=None, max_length=255)
    metadata: dict[str, str] = Field(default_factory=dict, max_length=20)

    @field_validator("metadata")
    @classmethod
    def _bounded_metadata(cls, value: dict[str, str]) -> dict[str, str]:
        for key, item in value.items():
            if not 1 <= len(key) <= 40 or len(item) > 500:
                msg = "metadata : clés de 1 à 40 caractères, valeurs de 500 caractères au plus"
                raise ValueError(msg)
        return value


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
)
async def create_payment_intent(
    body: PaymentIntentCreate,
    request: Request,
    principal: SecretKeyDep,
    session: SessionDep,
    resources: ResourcesDep,
    idempotency_key: IdempotencyKeyHeader = None,
) -> Response:
    key = require_key(idempotency_key)
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
)
async def confirm_payment_intent(
    intent_id: IntentId,
    request: Request,
    principal: SecretKeyDep,
    session: SessionDep,
    idempotency_key: IdempotencyKeyHeader = None,
) -> Response:
    key = require_key(idempotency_key)

    async def effect() -> tuple[int, BaseModel]:
        intent = await payments.confirm_intent(
            session, _scope(principal), intent_id, actor=_actor(principal)
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
