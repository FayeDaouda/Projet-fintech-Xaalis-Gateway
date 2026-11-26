"""Règlements par intent (au plus un) et virements."""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    String,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from xaalis.infra.db import Base


class Settlement(Base):
    """Clé primaire = intent : un paiement n'est réglé qu'une seule fois."""

    __tablename__ = "settlements"

    intent_id: Mapped[str] = mapped_column(ForeignKey("payment_intents.id"), primary_key=True)
    business_date: Mapped[date] = mapped_column(Date)
    amount: Mapped[int] = mapped_column(BigInteger)
    ledger_transaction_id: Mapped[str | None] = mapped_column(String(40))
    reconciliation_run_id: Mapped[str | None] = mapped_column(ForeignKey("reconciliation_runs.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Payout(Base):
    __tablename__ = "payouts"
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
    status: Mapped[str] = mapped_column(String(16))
    ledger_transaction_id: Mapped[str] = mapped_column(String(40))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
