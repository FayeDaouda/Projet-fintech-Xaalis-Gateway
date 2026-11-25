"""Callbacks opérateurs reçus et alertes de rapprochement."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, Boolean, DateTime, Identity, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from xaalis.infra.db import Base


class OperatorCallback(Base):
    __tablename__ = "operator_callbacks"
    __table_args__ = (UniqueConstraint("operator", "operator_reference"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    operator: Mapped[str] = mapped_column(String(16))
    operator_reference: Mapped[str] = mapped_column(String(64))
    intent_id: Mapped[str | None] = mapped_column(String(40))
    outcome: Mapped[str] = mapped_column(String(16))
    amount: Mapped[int] = mapped_column(BigInteger)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    disposition: Mapped[str] = mapped_column(String(24))
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ReconciliationAlert(Base):
    __tablename__ = "reconciliation_alerts"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    kind: Mapped[str] = mapped_column(String(32))
    operator: Mapped[str] = mapped_column(String(16))
    operator_reference: Mapped[str | None] = mapped_column(String(64))
    intent_id: Mapped[str | None] = mapped_column(String(40))
    livemode: Mapped[bool | None] = mapped_column(Boolean)
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
