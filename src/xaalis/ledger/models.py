"""Tables du grand livre : comptes, transactions comptables, écritures.

Les invariants critiques sont portés par PostgreSQL (migration ``0002_ledger``) :
immutabilité, équilibre différé au COMMIT, étanchéité test/live par clés composites.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Index,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from xaalis.infra.db import Base

ACCOUNT_KINDS = (
    "operator_clearing",
    "merchant_pending",
    "merchant_available",
    "merchant_payout",
    "xaalis_revenue",
)
TRANSACTION_KINDS = ("payment_capture", "settlement", "refund", "payout", "reversal")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class LedgerAccount(Base):
    __tablename__ = "ledger_accounts"
    __table_args__ = (
        UniqueConstraint("code", "livemode"),
        UniqueConstraint("id", "livemode"),  # cible des clés étrangères composites
        CheckConstraint(_in("kind", ACCOUNT_KINDS), name="kind"),
        CheckConstraint(
            "(kind = 'operator_clearing') = (operator IS NOT NULL)", name="operator_iff_clearing"
        ),
        CheckConstraint(
            "(kind LIKE 'merchant\\_%') = (merchant_id IS NOT NULL)", name="merchant_iff_merchant"
        ),
        ForeignKeyConstraint(
            ["merchant_id", "livemode"],
            ["merchant_environments.merchant_id", "merchant_environments.livemode"],
            ondelete="RESTRICT",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    code: Mapped[str] = mapped_column(String(80))
    kind: Mapped[str] = mapped_column(String(32))
    livemode: Mapped[bool] = mapped_column(Boolean)
    merchant_id: Mapped[str | None] = mapped_column(String(40))
    operator: Mapped[str | None] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class LedgerTransaction(Base):
    __tablename__ = "ledger_transactions"
    __table_args__ = (
        UniqueConstraint("kind", "reference"),  # une comptabilisation par fait métier
        UniqueConstraint("id", "livemode"),
        CheckConstraint("id LIKE 'ltx\\_%'", name="id_prefix"),
        CheckConstraint(_in("kind", TRANSACTION_KINDS), name="kind"),
        CheckConstraint("(kind = 'reversal') = (reverses_id IS NOT NULL)", name="reversal_link"),
    )

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    kind: Mapped[str] = mapped_column(String(32))
    reference: Mapped[str] = mapped_column(String(64))
    livemode: Mapped[bool] = mapped_column(Boolean)
    description: Mapped[str | None] = mapped_column(String(255))
    reverses_id: Mapped[str | None] = mapped_column(ForeignKey("ledger_transactions.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class LedgerEntry(Base):
    __tablename__ = "ledger_entries"
    __table_args__ = (
        CheckConstraint("amount <> 0", name="amount_non_zero"),
        ForeignKeyConstraint(
            ["transaction_id", "livemode"],
            ["ledger_transactions.id", "ledger_transactions.livemode"],
        ),
        ForeignKeyConstraint(
            ["account_id", "livemode"],
            ["ledger_accounts.id", "ledger_accounts.livemode"],
        ),
        Index("ix_ledger_entries_transaction_id", "transaction_id"),
        # Index couvrant : un solde se calcule par index-only scan (ADR 0001 §5).
        Index("ix_ledger_entries_account_id", "account_id", postgresql_include=["amount"]),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    transaction_id: Mapped[str] = mapped_column(String(40))
    account_id: Mapped[int] = mapped_column(BigInteger)
    livemode: Mapped[bool] = mapped_column(Boolean)
    amount: Mapped[int] = mapped_column(BigInteger)  # débit > 0, crédit < 0
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
