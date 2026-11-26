"""Exécutions de rapprochement et leurs lignes (rapprochées ou en écart)."""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Identity,
    Integer,
    LargeBinary,
    String,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from xaalis.infra.db import Base


class ReconciliationRun(Base):
    __tablename__ = "reconciliation_runs"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    statement_date: Mapped[date] = mapped_column(Date)
    operator: Mapped[str] = mapped_column(String(16))
    source: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16), default="pending")
    statement_csv: Mapped[bytes | None] = mapped_column(LargeBinary)
    statement_lines: Mapped[int | None] = mapped_column(Integer)
    matched: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(String(500))
    requested_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ReconciliationLine(Base):
    __tablename__ = "reconciliation_lines"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("reconciliation_runs.id"))
    outcome: Mapped[str] = mapped_column(String(24))
    operator_reference: Mapped[str] = mapped_column(String(64))
    intent_id: Mapped[str | None] = mapped_column(String(40))
    livemode: Mapped[bool | None] = mapped_column(Boolean)
    statement_amount: Mapped[int | None] = mapped_column(BigInteger)
    ledger_amount: Mapped[int | None] = mapped_column(BigInteger)
    occurrences: Mapped[int] = mapped_column(Integer, default=1)
