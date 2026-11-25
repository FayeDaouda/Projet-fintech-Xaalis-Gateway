"""Clés d'idempotence : conservées 24 h, une par (marchand, environnement, clé)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKeyConstraint, Integer, LargeBinary, String, func
from sqlalchemy.orm import Mapped, mapped_column

from xaalis.infra.db import Base


class IdempotencyRecord(Base):
    __tablename__ = "idempotency_keys"
    __table_args__ = (
        ForeignKeyConstraint(
            ["merchant_id", "livemode"],
            ["merchant_environments.merchant_id", "merchant_environments.livemode"],
            ondelete="CASCADE",
        ),
    )

    merchant_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    livemode: Mapped[bool] = mapped_column(Boolean, primary_key=True)
    key: Mapped[str] = mapped_column(String(255), primary_key=True)
    request_fingerprint: Mapped[bytes] = mapped_column(LargeBinary(32))
    response_status: Mapped[int | None] = mapped_column(Integer)
    response_body: Mapped[bytes | None] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
