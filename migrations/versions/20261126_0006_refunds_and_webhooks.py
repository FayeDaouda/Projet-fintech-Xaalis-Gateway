"""Remboursements (XG-08 à XG-11) et webhooks marchands (lot 5).

Revision ID: 0006_refunds_and_webhooks
Revises: 0005_operators
Create Date: 2026-11-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY, JSONB

revision: str = "0006_refunds_and_webhooks"
down_revision: str | None = "0005_operators"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _env_fk() -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["merchant_id", "livemode"],
        ["merchant_environments.merchant_id", "merchant_environments.livemode"],
        ondelete="RESTRICT",
    )


def _now(name: str = "created_at") -> sa.Column[sa.DateTime]:
    return sa.Column(name, sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)


def upgrade() -> None:
    op.create_table(
        "refunds",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("intent_id", sa.String(40), sa.ForeignKey("payment_intents.id"), nullable=False),
        sa.Column("merchant_id", sa.String(40), nullable=False),
        sa.Column("livemode", sa.Boolean, nullable=False),
        sa.Column("amount", sa.BigInteger, nullable=False),
        sa.Column("from_pending", sa.BigInteger, nullable=False),
        sa.Column("from_available", sa.BigInteger, nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("reason", sa.String(255)),
        sa.Column("ledger_transaction_id", sa.String(40), nullable=False),
        _now(),
        _env_fk(),
        sa.CheckConstraint("id LIKE 're\\_%'", name="id_prefix"),
        sa.CheckConstraint("amount > 0", name="amount_positive"),
        sa.CheckConstraint(
            "from_pending >= 0 AND from_available >= 0 AND from_pending + from_available = amount",
            name="funding_adds_up",
        ),
        sa.CheckConstraint("status = 'succeeded'", name="status"),
    )
    op.create_index("ix_refunds_intent_id", "refunds", ["intent_id"])

    op.create_table(
        "webhook_endpoints",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("merchant_id", sa.String(40), nullable=False),
        sa.Column("livemode", sa.Boolean, nullable=False),
        sa.Column("url", sa.String(2048), nullable=False),
        sa.Column("events", ARRAY(sa.String(32)), nullable=False),
        # Secret de signature chiffré (AES via Fernet) : il faut pouvoir le relire pour signer.
        sa.Column("secret_ciphertext", sa.LargeBinary, nullable=False),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.true()),
        _now(),
        _env_fk(),
        sa.CheckConstraint("id LIKE 'we\\_%'", name="id_prefix"),
        sa.CheckConstraint("cardinality(events) > 0", name="events_not_empty"),
    )
    op.create_index(
        "ix_webhook_endpoints_merchant", "webhook_endpoints", ["merchant_id", "livemode"]
    )

    op.create_table(
        "webhook_events",
        sa.Column("id", sa.String(40), primary_key=True),  # evt_… = event_id de l'outbox
        sa.Column("merchant_id", sa.String(40), nullable=False),
        sa.Column("livemode", sa.Boolean, nullable=False),
        sa.Column("type", sa.String(64), nullable=False),
        sa.Column("payload", JSONB, nullable=False),
        sa.Column("request_id", sa.String(128)),
        _now(),
        _env_fk(),
    )

    op.create_table(
        "webhook_deliveries",
        sa.Column("id", sa.BigInteger, sa.Identity(always=True), primary_key=True),
        sa.Column("event_id", sa.String(40), sa.ForeignKey("webhook_events.id"), nullable=False),
        sa.Column(
            "endpoint_id", sa.String(40), sa.ForeignKey("webhook_endpoints.id"), nullable=False
        ),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("locked_until", sa.DateTime(timezone=True)),
        sa.Column("last_status_code", sa.Integer),
        sa.Column("last_error", sa.String(500)),
        sa.Column("delivered_at", sa.DateTime(timezone=True)),
        _now(),
        sa.UniqueConstraint("event_id", "endpoint_id"),
        sa.CheckConstraint("status IN ('pending', 'delivered', 'dead_letter')", name="status"),
    )
    op.create_index(
        "ix_webhook_deliveries_due",
        "webhook_deliveries",
        ["next_attempt_at"],
        postgresql_where=sa.text("status = 'pending'"),
    )


def downgrade() -> None:
    op.drop_table("webhook_deliveries")
    op.drop_table("webhook_events")
    op.drop_table("webhook_endpoints")
    op.drop_table("refunds")
