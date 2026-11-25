"""Tables des intents de paiement et de leur historique.

Les règles métier sont doublées par des contraintes en base (migration 0004) :
bornes du montant (XG-01), devise (XG-02), format du numéro (XG-03), bornes de la
commission (XG-05), et ``amount_refunded ≤ amount`` (XG-09).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from xaalis.infra.db import Base


class PaymentIntent(Base):
    __tablename__ = "payment_intents"
    __table_args__ = (
        ForeignKeyConstraint(
            ["merchant_id", "livemode"],
            ["merchant_environments.merchant_id", "merchant_environments.livemode"],
            ondelete="RESTRICT",
        ),
    )

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    merchant_id: Mapped[str] = mapped_column(String(40))
    livemode: Mapped[bool] = mapped_column(Boolean)
    amount: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(String(3))
    fee: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(24))
    payer_phone: Mapped[str] = mapped_column(String(16))
    operator: Mapped[str | None] = mapped_column(String(16))
    operator_reference: Mapped[str | None] = mapped_column(String(64))
    amount_refunded: Mapped[int] = mapped_column(BigInteger, default=0)
    description: Mapped[str | None] = mapped_column(String(255))
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    succeeded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    @property
    def net(self) -> int:
        return self.amount - self.fee


class PaymentEvent(Base):
    """Historique en ajout seul de chaque transition (ancien état, nouvel état, cause, acteur)."""

    __tablename__ = "payment_events"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    intent_id: Mapped[str] = mapped_column(ForeignKey("payment_intents.id"))
    from_status: Mapped[str | None] = mapped_column(String(24))
    to_status: Mapped[str] = mapped_column(String(24))
    cause: Mapped[str] = mapped_column(String(32))
    actor: Mapped[str] = mapped_column(String(64))
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
