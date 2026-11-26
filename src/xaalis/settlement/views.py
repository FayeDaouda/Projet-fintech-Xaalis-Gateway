"""Représentation publique d'un virement (API et webhook ``payout.paid``)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

from xaalis.core.money import Currency
from xaalis.settlement.models import Payout


class PayoutView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    object: Literal["payout"] = "payout"
    livemode: bool
    amount: int
    currency: Currency
    status: Literal["paid"]
    created_at: datetime

    @classmethod
    def of(cls, payout: Payout) -> PayoutView:
        return cls(
            id=payout.id,
            livemode=payout.livemode,
            amount=payout.amount,
            currency="XOF",
            status="paid",
            created_at=payout.created_at,
        )
