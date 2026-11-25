"""Réception des callbacks opérateurs (lot 3).

Pas de clé d'API : l'appelant est authentifié par la **signature** propre à chaque
opérateur. Tout callback correctement signé reçoit 200 — même s'il est dupliqué, tardif
ou incohérent — pour que l'opérateur cesse de le renvoyer ; l'anomalie est enregistrée
comme alerte de rapprochement.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Request

from xaalis.api.problems import Problem
from xaalis.api.resources import ResourcesDep, SessionDep
from xaalis.api.schemas import ApiModel
from xaalis.core.operators import Operator
from xaalis.observability.metrics import OPERATOR_CALLBACKS
from xaalis.operators.callbacks import parse_callback
from xaalis.operators.service import Disposition, handle_callback

router = APIRouter(prefix="/v1/operators", tags=["operators"])


class CallbackAck(ApiModel):
    received: Literal[True] = True
    disposition: Disposition


@router.post(
    "/{operator}/callback",
    response_model=CallbackAck,
    summary="Callback asynchrone d'un opérateur (signature propre à chaque opérateur)",
    responses={
        400: {"model": Problem, "description": "Callback mal formé."},
        401: {"model": Problem, "description": "Signature invalide."},
    },
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {"schema": {"type": "object"}},
                "application/x-www-form-urlencoded": {"schema": {"type": "object"}},
            },
        }
    },
)
async def operator_callback(
    operator: Operator, request: Request, session: SessionDep, resources: ResourcesDep
) -> CallbackAck:
    body = await request.body()
    headers = {name.lower(): value for name, value in request.headers.items()}
    callback = parse_callback(
        operator, headers, body, resources.settings.operator_secret(operator.value)
    )
    async with session.begin():
        disposition = await handle_callback(session, callback)
    OPERATOR_CALLBACKS.labels(operator=operator.value, disposition=disposition.value).inc()
    if disposition is Disposition.APPLIED:
        await resources.operator_health.record_outcome(
            operator, succeeded=callback.outcome.value == "succeeded"
        )
    return CallbackAck(disposition=disposition)
