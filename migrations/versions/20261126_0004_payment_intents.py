"""Intents de paiement, historique des transitions, idempotence, outbox.

Revision ID: 0004_payment_intents
Revises: 0003_api_keys_and_admins
Create Date: 2026-11-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0004_payment_intents"
down_revision: str | None = "0003_api_keys_and_admins"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STATUSES = (
    "'created', 'processing', 'succeeded', 'failed', 'canceled', 'expired', "
    "'partially_refunded', 'refunded'"
)
OPERATORS = "'gopay', 'teranga', 'ndiaga'"


def _now(name: str = "created_at") -> sa.Column[sa.DateTime]:
    return sa.Column(name, sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)


def upgrade() -> None:
    op.create_table(
        "payment_intents",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("merchant_id", sa.String(40), nullable=False),
        sa.Column("livemode", sa.Boolean, nullable=False),
        sa.Column("amount", sa.BigInteger, nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("fee", sa.BigInteger, nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("payer_phone", sa.String(16), nullable=False),
        sa.Column("operator", sa.String(16)),
        sa.Column("operator_reference", sa.String(64)),
        sa.Column("amount_refunded", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("description", sa.String(255)),
        sa.Column("metadata", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        _now(),
        _now("updated_at"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("succeeded_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(
            ["merchant_id", "livemode"],
            ["merchant_environments.merchant_id", "merchant_environments.livemode"],
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("operator", "operator_reference"),
        sa.CheckConstraint("id LIKE 'pi\\_%'", name="id_prefix"),
        sa.CheckConstraint("amount BETWEEN 100 AND 2000000", name="amount_bounds"),  # XG-01
        sa.CheckConstraint("currency = 'XOF'", name="currency_xof"),  # XG-02
        sa.CheckConstraint(
            "payer_phone ~ '^\\+2217[0-9]{8}$'", name="payer_phone_e164_sn"
        ),  # XG-03
        sa.CheckConstraint(
            "fee BETWEEN 100 AND 5000 AND fee <= amount", name="fee_bounds"
        ),  # XG-05
        sa.CheckConstraint(f"status IN ({STATUSES})", name="status"),
        sa.CheckConstraint(f"operator IS NULL OR operator IN ({OPERATORS})", name="operator"),
        sa.CheckConstraint(
            "metadata IS NOT NULL AND jsonb_typeof(metadata) = 'object'", name="metadata_object"
        ),
        # XG-09 en base : on ne rembourse jamais plus que le montant initial.
        sa.CheckConstraint("amount_refunded BETWEEN 0 AND amount", name="refund_bounds"),
        # Cohérence statut ↔ montant remboursé : aucun état incohérent n'est représentable.
        sa.CheckConstraint(
            "(status = 'refunded' AND amount_refunded = amount)"
            " OR (status = 'partially_refunded' AND amount_refunded > 0"
            "     AND amount_refunded < amount)"
            " OR (status NOT IN ('refunded', 'partially_refunded') AND amount_refunded = 0)",
            name="refund_status_consistency",
        ),
        sa.CheckConstraint(
            "status NOT IN ('succeeded', 'partially_refunded', 'refunded')"
            " OR succeeded_at IS NOT NULL",
            name="succeeded_at_required",
        ),
        sa.CheckConstraint(
            "status NOT IN ('processing', 'succeeded', 'failed', 'partially_refunded', 'refunded')"
            " OR operator IS NOT NULL",
            name="operator_required_once_confirmed",
        ),
    )
    op.create_index(
        "ix_payment_intents_merchant_listing", "payment_intents", ["merchant_id", "livemode", "id"]
    )
    op.create_index(
        "ix_payment_intents_merchant_status",
        "payment_intents",
        ["merchant_id", "livemode", "status", "id"],
    )
    # Job d'expiration : ne parcourt que les intents encore vivants.
    op.create_index(
        "ix_payment_intents_expirable",
        "payment_intents",
        ["expires_at"],
        postgresql_where=sa.text("status IN ('created', 'processing')"),
    )
    # XG-14 : comptage des intents en cours d'un payeur.
    op.create_index(
        "ix_payment_intents_payer_processing",
        "payment_intents",
        ["payer_phone", "livemode"],
        postgresql_where=sa.text("status = 'processing'"),
    )

    op.create_table(
        "payment_events",
        sa.Column("id", sa.BigInteger, sa.Identity(always=True), primary_key=True),
        sa.Column("intent_id", sa.String(40), sa.ForeignKey("payment_intents.id"), nullable=False),
        sa.Column("from_status", sa.String(24)),
        sa.Column("to_status", sa.String(24), nullable=False),
        sa.Column("cause", sa.String(32), nullable=False),
        sa.Column("actor", sa.String(64), nullable=False),
        sa.Column("details", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        _now(),
        sa.CheckConstraint(f"to_status IN ({STATUSES})", name="to_status"),
    )
    op.create_index("ix_payment_events_intent_id", "payment_events", ["intent_id", "id"])
    op.execute(
        """
        CREATE FUNCTION append_only_violation() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION '% sur % est interdit (historique en ajout seul)', TG_OP, TG_TABLE_NAME
                USING ERRCODE = 'XA001';
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_payment_events_append_only
        BEFORE UPDATE OR DELETE ON payment_events
        FOR EACH ROW EXECUTE FUNCTION append_only_violation();
        """
    )

    op.create_table(
        "idempotency_keys",
        sa.Column("merchant_id", sa.String(40), primary_key=True),
        sa.Column("livemode", sa.Boolean, primary_key=True),
        sa.Column("key", sa.String(255), primary_key=True),
        sa.Column("request_fingerprint", sa.LargeBinary(32), nullable=False),
        sa.Column("response_status", sa.Integer),
        sa.Column("response_body", sa.LargeBinary),
        _now(),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["merchant_id", "livemode"],
            ["merchant_environments.merchant_id", "merchant_environments.livemode"],
            ondelete="CASCADE",
        ),
        sa.CheckConstraint("char_length(key) BETWEEN 1 AND 255", name="key_length"),
    )
    op.create_index("ix_idempotency_keys_expires_at", "idempotency_keys", ["expires_at"])

    op.create_table(
        "outbox_messages",
        sa.Column("id", sa.BigInteger, sa.Identity(always=True), primary_key=True),
        sa.Column("event_id", sa.String(40), nullable=False),
        sa.Column("topic", sa.String(64), nullable=False),
        sa.Column("merchant_id", sa.String(40), nullable=False),
        sa.Column("livemode", sa.Boolean, nullable=False),
        sa.Column("aggregate_id", sa.String(40), nullable=False),
        sa.Column("payload", JSONB, nullable=False),
        sa.Column("request_id", sa.String(128)),
        _now(),
        sa.Column("published_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("event_id"),
        sa.CheckConstraint("event_id LIKE 'evt\\_%'", name="event_id_prefix"),
    )
    op.create_index(
        "ix_outbox_messages_unpublished",
        "outbox_messages",
        ["id"],
        postgresql_where=sa.text("published_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_table("outbox_messages")
    op.drop_table("idempotency_keys")
    op.drop_table("payment_events")
    op.execute("DROP FUNCTION append_only_violation()")
    op.drop_table("payment_intents")
