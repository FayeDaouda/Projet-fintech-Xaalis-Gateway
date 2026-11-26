"""Échéance d'un intent calculée par PostgreSQL à l'insertion (XG-07 : 15 minutes).

L'INSERT n'a plus besoin d'un « SELECT now() » préalable : ``created_at`` et ``expires_at``
sont des valeurs par défaut de la base, relues dans le RETURNING (docs/load-test.md).
La durée doit rester égale à ``xaalis.payments.service.INTENT_TTL`` (test XG-07).

Revision ID: 0008_intent_expiry_default
Revises: 0007_reconciliation
Create Date: 2026-11-27
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0008_intent_expiry_default"
down_revision: str | None = "0007_reconciliation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE payment_intents ALTER COLUMN expires_at "
        "SET DEFAULT now() + interval '15 minutes'"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE payment_intents ALTER COLUMN expires_at DROP DEFAULT")
