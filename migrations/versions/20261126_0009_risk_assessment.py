"""Évaluation anti-fraude de l'intent (bonus gRPC, ADR 0013).

Score en pour mille (entier, XG-01), décision et source (service ou repli). NULL tant que
le worker n'a pas évalué le paiement ; renseigné une seule fois, avant le premier débit.

Revision ID: 0009_risk_assessment
Revises: 0008_intent_expiry_default
Create Date: 2026-11-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009_risk_assessment"
down_revision: str | None = "0008_intent_expiry_default"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Noms déjà complets : op.f() évite que la convention de nommage les préfixe une 2e fois.
    op.add_column("payment_intents", sa.Column("risk_score", sa.SmallInteger(), nullable=True))
    op.add_column("payment_intents", sa.Column("risk_decision", sa.String(16), nullable=True))
    op.add_column("payment_intents", sa.Column("risk_source", sa.String(16), nullable=True))
    op.create_check_constraint(
        op.f("ck_payment_intents_risk_score_range"),
        "payment_intents",
        "risk_score IS NULL OR risk_score BETWEEN 0 AND 1000",
    )
    op.create_check_constraint(
        op.f("ck_payment_intents_risk_decision"),
        "payment_intents",
        "risk_decision IS NULL OR risk_decision IN ('approve', 'review', 'decline')",
    )
    op.create_check_constraint(
        op.f("ck_payment_intents_risk_source"),
        "payment_intents",
        "(risk_decision IS NULL AND risk_source IS NULL) "
        "OR risk_source IN ('service', 'fallback')",
    )


def downgrade() -> None:
    for name in ("risk_source", "risk_decision", "risk_score_range"):
        op.drop_constraint(op.f(f"ck_payment_intents_{name}"), "payment_intents", type_="check")
    for column in ("risk_source", "risk_decision", "risk_score"):
        op.drop_column("payment_intents", column)
