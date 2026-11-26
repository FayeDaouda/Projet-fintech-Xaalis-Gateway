"""Rapprochement des relevés, règlement T+1 et virements (lot 6).

Revision ID: 0007_reconciliation
Revises: 0006_refunds_and_webhooks
Create Date: 2026-11-27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_reconciliation"
down_revision: str | None = "0006_refunds_and_webhooks"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OUTCOMES = "'matched', 'missing_in_ledger', 'missing_in_statement', 'amount_mismatch', 'duplicate'"


def _now(name: str = "created_at") -> sa.Column[sa.DateTime]:
    return sa.Column(name, sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)


def upgrade() -> None:
    op.create_table(
        "reconciliation_runs",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("statement_date", sa.Date, nullable=False),
        sa.Column("operator", sa.String(16), nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("statement_csv", sa.LargeBinary),
        sa.Column("statement_lines", sa.Integer),
        sa.Column("matched", sa.Integer),
        sa.Column("error", sa.String(500)),
        sa.Column("requested_by", sa.String(64), nullable=False),
        _now(),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("id LIKE 'rec\\_%'", name="id_prefix"),
        sa.CheckConstraint("source IN ('simulator', 'upload')", name="source"),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'completed', 'failed')", name="status"
        ),
    )
    op.create_index(
        "ix_reconciliation_runs_date_operator",
        "reconciliation_runs",
        ["statement_date", "operator", "created_at"],
    )
    op.create_index(
        "ix_reconciliation_runs_pending",
        "reconciliation_runs",
        ["created_at"],
        postgresql_where=sa.text("status = 'pending'"),
    )

    op.create_table(
        "reconciliation_lines",
        sa.Column("id", sa.BigInteger, sa.Identity(always=True), primary_key=True),
        sa.Column("run_id", sa.String(40), sa.ForeignKey("reconciliation_runs.id"), nullable=False),
        sa.Column("outcome", sa.String(24), nullable=False),
        sa.Column("operator_reference", sa.String(64), nullable=False),
        sa.Column("intent_id", sa.String(40)),
        sa.Column("livemode", sa.Boolean),
        sa.Column("statement_amount", sa.BigInteger),
        sa.Column("ledger_amount", sa.BigInteger),
        sa.Column("occurrences", sa.Integer, nullable=False, server_default="1"),
        sa.CheckConstraint(f"outcome IN ({OUTCOMES})", name="outcome"),
    )
    op.create_index("ix_reconciliation_lines_run", "reconciliation_lines", ["run_id", "outcome"])
    op.create_index("ix_reconciliation_lines_intent", "reconciliation_lines", ["intent_id"])

    op.create_table(
        "settlements",
        sa.Column(
            "intent_id", sa.String(40), sa.ForeignKey("payment_intents.id"), primary_key=True
        ),
        sa.Column("business_date", sa.Date, nullable=False),
        sa.Column("amount", sa.BigInteger, nullable=False),
        sa.Column("ledger_transaction_id", sa.String(40)),
        sa.Column("reconciliation_run_id", sa.String(40), sa.ForeignKey("reconciliation_runs.id")),
        _now(),
        sa.CheckConstraint("amount >= 0", name="amount_non_negative"),
        sa.CheckConstraint(
            "(amount = 0) = (ledger_transaction_id IS NULL)", name="posting_iff_amount"
        ),
    )

    op.create_table(
        "payouts",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("merchant_id", sa.String(40), nullable=False),
        sa.Column("livemode", sa.Boolean, nullable=False),
        sa.Column("amount", sa.BigInteger, nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("ledger_transaction_id", sa.String(40), nullable=False),
        _now(),
        sa.ForeignKeyConstraint(
            ["merchant_id", "livemode"],
            ["merchant_environments.merchant_id", "merchant_environments.livemode"],
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint("id LIKE 'po\\_%'", name="id_prefix"),
        sa.CheckConstraint("amount > 0", name="amount_positive"),
        sa.CheckConstraint("status = 'paid'", name="status"),
    )


def downgrade() -> None:
    op.drop_table("payouts")
    op.drop_table("settlements")
    op.drop_table("reconciliation_lines")
    op.drop_table("reconciliation_runs")
