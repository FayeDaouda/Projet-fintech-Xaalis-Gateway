"""Marchands et environnements test/live.

Revision ID: 0001_merchants
Revises:
Create Date: 2026-11-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001_merchants"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "merchants",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("id LIKE 'm\\_%'", name="id_prefix"),
        sa.CheckConstraint("char_length(name) BETWEEN 1 AND 200", name="name_length"),
    )
    op.create_table(
        "merchant_environments",
        sa.Column(
            "merchant_id",
            sa.String(40),
            sa.ForeignKey("merchants.id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column("livemode", sa.Boolean, primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )


def downgrade() -> None:
    op.drop_table("merchant_environments")
    op.drop_table("merchants")
