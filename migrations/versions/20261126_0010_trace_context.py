"""Contexte de trace W3C (``traceparent``) transporté par l'outbox (bonus, ADR 0014).

Écrit dans la même transaction que l'événement, comme le ``request_id`` : le worker reprend
ainsi la trace de la requête d'origine (débit opérateur, livraison de webhook).

Revision ID: 0010_trace_context
Revises: 0009_risk_assessment
Create Date: 2026-11-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010_trace_context"
down_revision: str | None = "0009_risk_assessment"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# « 00-<trace id 32 hex>-<span id 16 hex>-<flags 2 hex> » : 55 caractères (W3C Trace Context).
TRACEPARENT = sa.String(64)


def upgrade() -> None:
    op.add_column("outbox_messages", sa.Column("traceparent", TRACEPARENT, nullable=True))
    op.add_column("webhook_events", sa.Column("traceparent", TRACEPARENT, nullable=True))


def downgrade() -> None:
    op.drop_column("webhook_events", "traceparent")
    op.drop_column("outbox_messages", "traceparent")
