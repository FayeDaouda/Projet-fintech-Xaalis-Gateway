"""Représentation publique d'un remboursement (réponse API et webhook ``refund.succeeded``)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from xaalis.core.money import Currency
from xaalis.refunds.models import Refund


class RefundView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(examples=["re_01JD8Q3W7Z4K2N6R5T9V0XBCYA"])
    object: Literal["refund"] = "refund"
    livemode: bool
    payment_intent: str
    amount: int
    currency: Currency
    status: Literal["succeeded"]
    reason: str | None
    created_at: datetime

    @classmethod
    def of(cls, refund: Refund) -> RefundView:
        return cls(
            id=refund.id,
            livemode=refund.livemode,
            payment_intent=refund.intent_id,
            amount=refund.amount,
            currency="XOF",
            status="succeeded",
            reason=refund.reason,
            created_at=refund.created_at,
        )
