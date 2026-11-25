"""Clés d'API (empreintes uniquement) et comptes administrateurs.

Revision ID: 0003_api_keys_and_admins
Revises: 0002_ledger
Create Date: 2026-11-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_api_keys_and_admins"
down_revision: str | None = "0002_ledger"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "api_keys",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("merchant_id", sa.String(40), nullable=False),
        sa.Column("livemode", sa.Boolean, nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("fingerprint", sa.LargeBinary(32), nullable=False),
        sa.Column("redacted", sa.String(24), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("fingerprint"),
        sa.CheckConstraint("id LIKE 'key\\_%'", name="id_prefix"),
        sa.CheckConstraint("kind IN ('publishable', 'secret')", name="kind"),
        sa.CheckConstraint("octet_length(fingerprint) = 32", name="fingerprint_length"),
        sa.ForeignKeyConstraint(
            ["merchant_id", "livemode"],
            ["merchant_environments.merchant_id", "merchant_environments.livemode"],
            ondelete="RESTRICT",
        ),
    )
    op.create_index("ix_api_keys_merchant_id_livemode", "api_keys", ["merchant_id", "livemode"])

    op.create_table(
        "admin_users",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("email", sa.String(254), nullable=False),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("email"),
        sa.CheckConstraint("id LIKE 'adm\\_%'", name="id_prefix"),
        sa.CheckConstraint("email = lower(email)", name="email_lowercase"),
    )


def downgrade() -> None:
    op.drop_table("admin_users")
    op.drop_index("ix_api_keys_merchant_id_livemode", table_name="api_keys")
    op.drop_table("api_keys")
