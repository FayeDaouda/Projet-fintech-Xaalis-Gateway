"""Table des remboursements. ``from_pending + from_available = amount`` est vérifié en base."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, ForeignKeyConstraint, String, func
from sqlalchemy.orm import Mapped, mapped_column

from xaalis.infra.db import Base


class Refund(Base):
    __tablename__ = "refunds"
    __table_args__ = (
        ForeignKeyConstraint(
            ["merchant_id", "livemode"],
            ["merchant_environments.merchant_id", "merchant_environments.livemode"],
            ondelete="RESTRICT",
        ),
    )

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    intent_id: Mapped[str] = mapped_column(ForeignKey("payment_intents.id"))
    merchant_id: Mapped[str] = mapped_column(String(40))
    livemode: Mapped[bool] = mapped_column(Boolean)
    amount: Mapped[int] = mapped_column(BigInteger)
    from_pending: Mapped[int] = mapped_column(BigInteger)
    from_available: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(16))
    reason: Mapped[str | None] = mapped_column(String(255))
    ledger_transaction_id: Mapped[str] = mapped_column(String(40))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
