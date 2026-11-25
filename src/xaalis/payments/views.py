"""Représentation publique d'un intent : réponse de l'API **et** contenu des webhooks.

Une seule source de vérité : le champ ``payer_phone`` y est toujours masqué (XG-03),
et aucun champ interne (référence opérateur, numéro complet) n'y figure (OWASP API3).
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from xaalis.core.money import Currency
from xaalis.core.operators import Operator
from xaalis.core.phone import mask_phone
from xaalis.payments.models import PaymentIntent
from xaalis.payments.state_machine import IntentStatus


class PaymentIntentView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(examples=["pi_01JD8Q3W7Z4K2N6R5T9V0XBCYA"])
    object: Literal["payment_intent"] = "payment_intent"
    livemode: bool
    amount: int = Field(examples=[12345])
    currency: Currency
    fee: int = Field(examples=[223])
    net: int = Field(examples=[12122])
    status: IntentStatus
    payer_phone: str = Field(examples=["+221 77 *** ** 12"], description="Toujours masqué.")
    operator: Operator | None
    amount_refunded: int
    description: str | None = None
    metadata: dict[str, str]
    created_at: datetime
    expires_at: datetime
    succeeded_at: datetime | None = None

    @classmethod
    def of(cls, intent: PaymentIntent) -> PaymentIntentView:
        return cls(
            id=intent.id,
            livemode=intent.livemode,
            amount=intent.amount,
            currency="XOF",
            fee=intent.fee,
            net=intent.net,
            status=IntentStatus(intent.status),
            payer_phone=mask_phone(intent.payer_phone),
            operator=Operator(intent.operator) if intent.operator else None,
            amount_refunded=intent.amount_refunded,
            description=intent.description,
            metadata=dict(intent.metadata_),
            created_at=intent.created_at,
            expires_at=intent.expires_at,
            succeeded_at=intent.succeeded_at,
        )
