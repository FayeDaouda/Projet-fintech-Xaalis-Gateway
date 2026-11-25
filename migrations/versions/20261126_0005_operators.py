"""Lot 3 : callbacks opérateurs, alertes de rapprochement, bail de l'outbox.

Revision ID: 0005_operators
Revises: 0004_payment_intents
Create Date: 2026-11-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0005_operators"
down_revision: str | None = "0004_payment_intents"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OPERATORS = "'gopay', 'teranga', 'ndiaga'"
ALERT_KINDS = (
    "'late_callback', 'unexpected_callback', 'operator_mismatch', 'amount_mismatch', "
    "'unknown_intent'"
)


def upgrade() -> None:
    # Bail (lease) : un message est « réservé » par un worker le temps de son traitement,
    # sans garder de transaction ouverte pendant l'appel réseau.
    op.add_column(
        "outbox_messages", sa.Column("attempts", sa.Integer, nullable=False, server_default="0")
    )
    op.add_column(
        "outbox_messages",
        sa.Column(
            "available_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.add_column("outbox_messages", sa.Column("locked_until", sa.DateTime(timezone=True)))
    op.add_column("outbox_messages", sa.Column("last_error", sa.String(500)))
    op.create_index(
        "ix_outbox_messages_ready",
        "outbox_messages",
        ["topic", "available_at"],
        postgresql_where=sa.text("published_at IS NULL"),
    )

    # Journal brut des callbacks reçus : déduplication par référence opérateur unique.
    op.create_table(
        "operator_callbacks",
        sa.Column("id", sa.BigInteger, sa.Identity(always=True), primary_key=True),
        sa.Column("operator", sa.String(16), nullable=False),
        sa.Column("operator_reference", sa.String(64), nullable=False),
        sa.Column("intent_id", sa.String(40)),
        sa.Column("outcome", sa.String(16), nullable=False),
        sa.Column("amount", sa.BigInteger, nullable=False),
        sa.Column("payload", JSONB, nullable=False),
        sa.Column("disposition", sa.String(24), nullable=False),
        sa.Column(
            "received_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("operator", "operator_reference"),
        sa.CheckConstraint(f"operator IN ({OPERATORS})", name="operator"),
        sa.CheckConstraint("outcome IN ('succeeded', 'failed')", name="outcome"),
    )
    op.create_index("ix_operator_callbacks_intent_id", "operator_callbacks", ["intent_id"])

    # Alertes de rapprochement (XG-07 : callback après expiration, etc.), lues au lot 6.
    op.create_table(
        "reconciliation_alerts",
        sa.Column("id", sa.BigInteger, sa.Identity(always=True), primary_key=True),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("operator", sa.String(16), nullable=False),
        sa.Column("operator_reference", sa.String(64)),
        sa.Column("intent_id", sa.String(40)),
        sa.Column("livemode", sa.Boolean),
        sa.Column("details", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(f"kind IN ({ALERT_KINDS})", name="kind"),
    )
    op.create_index("ix_reconciliation_alerts_created_at", "reconciliation_alerts", ["created_at"])


def downgrade() -> None:
    op.drop_table("reconciliation_alerts")
    op.drop_table("operator_callbacks")
    op.drop_index("ix_outbox_messages_ready", table_name="outbox_messages")
    for column in ("last_error", "locked_until", "available_at", "attempts"):
        op.drop_column("outbox_messages", column)
