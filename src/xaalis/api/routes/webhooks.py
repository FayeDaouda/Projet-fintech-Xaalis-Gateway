"""Endpoints et événements webhooks du marchand (lot 5)."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal, cast

from fastapi import APIRouter, Path
from pydantic import Field

from xaalis.api.auth import SecretKeyDep
from xaalis.api.problems import Problem
from xaalis.api.resources import ResourcesDep, SessionDep
from xaalis.api.schemas import ApiModel
from xaalis.payments.service import Scope
from xaalis.webhooks import service as webhooks
from xaalis.webhooks.service import EventType

router = APIRouter(tags=["webhooks"])
EventId = Annotated[str, Path(pattern=r"^evt_[0-9A-HJKMNP-TV-Z]{26}$")]


class EndpointCreate(ApiModel):
    url: str = Field(
        min_length=10,
        max_length=2048,
        pattern=r"^https?://[A-Za-z0-9.-]+(:[0-9]{1,5})?(/[^ \t\n\r\f\v]*)?$",
        examples=["https://boutique.sn/webhooks"],
    )
    events: list[EventType] = Field(min_length=1, max_length=len(EventType))


class EndpointCreated(ApiModel):
    id: str
    object: Literal["webhook_endpoint"] = "webhook_endpoint"
    livemode: bool
    url: str
    events: list[EventType]
    secret: str = Field(description="Secret de signature, affiché une seule fois.")
    created_at: datetime


@router.post(
    "/v1/webhook-endpoints",
    status_code=201,
    response_model=EndpointCreated,
    summary="Enregistrer une URL de webhook (renvoie son secret une seule fois)",
    responses={422: {"model": Problem, "description": "URL invalide ou interdite (SSRF)."}},
)
async def create_endpoint(
    body: EndpointCreate, principal: SecretKeyDep, session: SessionDep, resources: ResourcesDep
) -> EndpointCreated:
    # Pas d'idempotence stockée : la réponse contient un secret en clair (ADR 0005).
    async with session.begin():
        created = await webhooks.create_endpoint(
            session,
            Scope(principal.merchant_id, principal.livemode),
            url=body.url,
            events=body.events,
            encryption_key=resources.settings.webhook_encryption_key.get_secret_value(),
        )
    endpoint = created.endpoint
    return EndpointCreated(
        id=endpoint.id,
        livemode=endpoint.livemode,
        url=endpoint.url,
        events=[EventType(e) for e in endpoint.events],
        secret=created.secret,
        created_at=endpoint.created_at,
    )


DeliveryStatus = Literal["pending", "delivered", "dead_letter"]


class DeliveryOut(ApiModel):
    endpoint_id: str
    status: DeliveryStatus
    attempts: int
    last_status_code: int | None
    last_error: str | None
    next_attempt_at: datetime | None
    delivered_at: datetime | None


class EventOut(ApiModel):
    id: str
    object: Literal["event"] = "event"
    type: str
    livemode: bool
    created_at: datetime
    data: dict[str, Any]
    deliveries: list[DeliveryOut]


@router.get(
    "/v1/webhook-events/{event_id}",
    response_model=EventOut,
    summary="Consulter un événement et l'état de ses livraisons",
    responses={404: {"model": Problem, "description": "Événement introuvable."}},
)
async def get_event(event_id: EventId, principal: SecretKeyDep, session: SessionDep) -> EventOut:
    event, deliveries = await webhooks.get_event(
        session, Scope(principal.merchant_id, principal.livemode), event_id
    )
    return EventOut(
        id=event.id,
        type=event.type,
        livemode=event.livemode,
        created_at=event.created_at,
        data={"object": event.payload},
        deliveries=[
            DeliveryOut(
                endpoint_id=d.endpoint_id,
                status=cast(DeliveryStatus, d.status),
                attempts=d.attempts,
                last_status_code=d.last_status_code,
                last_error=d.last_error,
                next_attempt_at=d.next_attempt_at if d.status == "pending" else None,
                delivered_at=d.delivered_at,
            )
            for d in deliveries
        ],
    )


class ReplayAccepted(ApiModel):
    object: Literal["webhook_replay"] = "webhook_replay"
    event_id: str
    deliveries_rescheduled: int


@router.post(
    "/v1/webhook-events/{event_id}/replay",
    status_code=202,
    response_model=ReplayAccepted,
    summary="Renvoyer un événement (y compris depuis la dead letter)",
    responses={404: {"model": Problem, "description": "Événement introuvable."}},
)
async def replay_event(
    event_id: EventId, principal: SecretKeyDep, session: SessionDep
) -> ReplayAccepted:
    async with session.begin():
        count = await webhooks.replay(
            session, Scope(principal.merchant_id, principal.livemode), event_id
        )
    return ReplayAccepted(event_id=event_id, deliveries_rescheduled=count)
