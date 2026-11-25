"""Grand livre en partie double : tables, immutabilité, sceau et équilibre (XG-16).

Voir docs/adr/0001-modele-du-grand-livre.md. Toutes les violations lèvent le SQLSTATE
applicatif ``XG016``, que le code reconnaît sans dépendre du texte du message.

Revision ID: 0002_ledger
Revises: 0001_merchants
Create Date: 2026-11-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_ledger"
down_revision: str | None = "0001_merchants"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ACCOUNT_KINDS = "'operator_clearing', 'merchant_pending', 'merchant_available', "
ACCOUNT_KINDS += "'merchant_payout', 'xaalis_revenue'"
TRANSACTION_KINDS = "'payment_capture', 'settlement', 'refund', 'payout', 'reversal'"

LEDGER_TABLES = ("ledger_accounts", "ledger_transactions", "ledger_entries")


def _timestamp() -> sa.Column[sa.DateTime]:
    return sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )


def upgrade() -> None:
    op.create_table(
        "ledger_accounts",
        sa.Column("id", sa.BigInteger, sa.Identity(always=True), primary_key=True),
        sa.Column("code", sa.String(80), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("livemode", sa.Boolean, nullable=False),
        sa.Column("merchant_id", sa.String(40)),
        sa.Column("operator", sa.String(16)),
        _timestamp(),
        sa.UniqueConstraint("code", "livemode"),
        sa.UniqueConstraint("id", "livemode"),
        sa.CheckConstraint(f"kind IN ({ACCOUNT_KINDS})", name="kind"),
        sa.CheckConstraint(
            "(kind = 'operator_clearing') = (operator IS NOT NULL)", name="operator_iff_clearing"
        ),
        sa.CheckConstraint(
            "(kind LIKE 'merchant\\_%') = (merchant_id IS NOT NULL)", name="merchant_iff_merchant"
        ),
        sa.ForeignKeyConstraint(
            ["merchant_id", "livemode"],
            ["merchant_environments.merchant_id", "merchant_environments.livemode"],
            ondelete="RESTRICT",
        ),
    )

    op.create_table(
        "ledger_transactions",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("reference", sa.String(64), nullable=False),
        sa.Column("livemode", sa.Boolean, nullable=False),
        sa.Column("description", sa.String(255)),
        sa.Column("reverses_id", sa.String(40), sa.ForeignKey("ledger_transactions.id")),
        _timestamp(),
        sa.UniqueConstraint("kind", "reference"),
        sa.UniqueConstraint("id", "livemode"),
        sa.CheckConstraint("id LIKE 'ltx\\_%'", name="id_prefix"),
        sa.CheckConstraint(f"kind IN ({TRANSACTION_KINDS})", name="kind"),
        sa.CheckConstraint("(kind = 'reversal') = (reverses_id IS NOT NULL)", name="reversal_link"),
    )

    # Identifiant de la transaction SQL qui a créé l'en-tête : sert au « sceau » (§2).
    # Colonne technique, volontairement absente du modèle ORM.
    op.execute(
        "ALTER TABLE ledger_transactions "
        "ADD COLUMN created_xact_id xid8 NOT NULL DEFAULT pg_current_xact_id()"
    )

    op.create_table(
        "ledger_entries",
        sa.Column("id", sa.BigInteger, sa.Identity(always=True), primary_key=True),
        sa.Column("transaction_id", sa.String(40), nullable=False),
        sa.Column("account_id", sa.BigInteger, nullable=False),
        sa.Column("livemode", sa.Boolean, nullable=False),
        sa.Column("amount", sa.BigInteger, nullable=False),
        _timestamp(),
        sa.CheckConstraint("amount <> 0", name="amount_non_zero"),
        sa.ForeignKeyConstraint(
            ["transaction_id", "livemode"],
            ["ledger_transactions.id", "ledger_transactions.livemode"],
        ),
        sa.ForeignKeyConstraint(
            ["account_id", "livemode"],
            ["ledger_accounts.id", "ledger_accounts.livemode"],
        ),
    )
    op.create_index("ix_ledger_entries_transaction_id", "ledger_entries", ["transaction_id"])
    op.create_index(
        "ix_ledger_entries_account_id",
        "ledger_entries",
        ["account_id"],
        postgresql_include=["amount"],
    )

    # 1) Immutabilité : ni UPDATE, ni DELETE, ni TRUNCATE sur le grand livre.
    op.execute(
        """
        CREATE FUNCTION ledger_forbid_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'XG-16 : % sur % est interdit (grand livre en ajout seul)',
                TG_OP, TG_TABLE_NAME
                USING ERRCODE = 'XG016',
                      HINT = 'Corriger par une transaction d''extourne (reversal).';
        END;
        $$;
        """
    )
    for table in LEDGER_TABLES:
        op.execute(
            f"""
            CREATE TRIGGER trg_{table}_immutable
            BEFORE UPDATE OR DELETE ON {table}
            FOR EACH ROW EXECUTE FUNCTION ledger_forbid_mutation();
            """
        )
        op.execute(
            f"""
            CREATE TRIGGER trg_{table}_no_truncate
            BEFORE TRUNCATE ON {table}
            FOR EACH STATEMENT EXECUTE FUNCTION ledger_forbid_mutation();
            """
        )

    # 2) Sceau : une écriture ne peut rejoindre qu'une transaction comptable créée dans la
    #    MÊME transaction SQL. Une fois validée, une transaction comptable est close.
    op.execute(
        """
        CREATE FUNCTION ledger_entry_requires_open_transaction() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM ledger_transactions t
                WHERE t.id = NEW.transaction_id
                  AND t.created_xact_id = pg_current_xact_id()
            ) THEN
                RAISE EXCEPTION
                    'XG-16 : la transaction comptable % est inexistante ou déjà scellée',
                    NEW.transaction_id
                    USING ERRCODE = 'XG016';
            END IF;
            RETURN NEW;
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_ledger_entries_sealed
        BEFORE INSERT ON ledger_entries
        FOR EACH ROW EXECUTE FUNCTION ledger_entry_requires_open_transaction();
        """
    )

    # 3) Équilibre, vérifié au COMMIT : somme nulle et au moins deux écritures.
    #    Grâce au sceau, vérifier chaque en-tête une fois suffit à couvrir toutes les écritures.
    op.execute(
        """
        CREATE FUNCTION ledger_check_transaction_balanced() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE
            total   numeric;
            entries integer;
        BEGIN
            SELECT COALESCE(SUM(amount), 0), COUNT(*) INTO total, entries
            FROM ledger_entries WHERE transaction_id = NEW.id;
            IF total <> 0 OR entries < 2 THEN
                RAISE EXCEPTION
                    'XG-16 : transaction comptable % déséquilibrée (somme = %, écritures = %)',
                    NEW.id, total, entries
                    USING ERRCODE = 'XG016';
            END IF;
            RETURN NULL;
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER trg_ledger_transactions_balanced
        AFTER INSERT ON ledger_transactions
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION ledger_check_transaction_balanced();
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER trg_ledger_transactions_balanced ON ledger_transactions")
    op.execute("DROP TRIGGER trg_ledger_entries_sealed ON ledger_entries")
    for table in LEDGER_TABLES:
        op.execute(f"DROP TRIGGER trg_{table}_no_truncate ON {table}")
        op.execute(f"DROP TRIGGER trg_{table}_immutable ON {table}")
    op.drop_table("ledger_entries")
    op.drop_table("ledger_transactions")
    op.drop_table("ledger_accounts")
    op.execute("DROP FUNCTION ledger_check_transaction_balanced()")
    op.execute("DROP FUNCTION ledger_entry_requires_open_transaction()")
    op.execute("DROP FUNCTION ledger_forbid_mutation()")
