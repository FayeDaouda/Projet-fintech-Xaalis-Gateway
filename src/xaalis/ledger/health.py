"""Contrôle de l'invariant XG-16 sur tout le grand livre.

Les requêtes sont exécutées dans une transaction REPEATABLE READ en lecture seule :
elles voient toutes le même instantané, donc les compteurs sont cohérents entre eux
même si des paiements sont comptabilisés pendant le contrôle.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import BigInteger, ColumnElement, cast, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from xaalis.ledger.models import LedgerEntry, LedgerTransaction

# Triggers posés par la migration 0002 ; s'il en manque un, l'immutabilité n'est plus garantie.
_LEDGER_TABLES = ("ledger_accounts", "ledger_transactions", "ledger_entries")
EXPECTED_TRIGGERS = frozenset(
    {
        *(f"trg_{table}_immutable" for table in _LEDGER_TABLES),
        *(f"trg_{table}_no_truncate" for table in _LEDGER_TABLES),
        "trg_ledger_entries_sealed",
        "trg_ledger_transactions_balanced",
    }
)
_MAX_REPORTED = 100


@dataclass(frozen=True, slots=True)
class LedgerHealth:
    total: int
    total_test: int
    total_live: int
    transactions_count: int
    entries_count: int
    unbalanced_transactions: list[str]
    missing_triggers: list[str]

    @property
    def balanced(self) -> bool:
        return (
            self.total == 0
            and self.total_test == 0
            and self.total_live == 0
            and not self.unbalanced_transactions
        )

    @property
    def immutability_enforced(self) -> bool:
        return not self.missing_triggers


async def check_ledger_health(session: AsyncSession) -> LedgerHealth:
    async with session.begin():
        await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))

        def summed(*conditions: ColumnElement[bool]) -> ColumnElement[int]:
            total: ColumnElement[int] = func.sum(LedgerEntry.amount)
            if conditions:
                total = func.sum(LedgerEntry.amount).filter(*conditions)
            return cast(func.coalesce(total, 0), BigInteger)

        # Un seul parcours des écritures pour les trois sommes et le comptage.
        totals = (
            await session.execute(
                select(
                    summed(),
                    summed(LedgerEntry.livemode.is_(False)),
                    summed(LedgerEntry.livemode.is_(True)),
                    func.count(LedgerEntry.id),
                )
            )
        ).one()
        total, total_test, total_live, entries_count = (int(value) for value in totals)
        transactions_count = int(
            await session.scalar(select(func.count(LedgerTransaction.id))) or 0
        )

        # Une transaction est fautive si sa somme est non nulle OU si elle a < 2 écritures.
        unbalanced = await session.scalars(
            select(LedgerTransaction.id)
            .outerjoin(LedgerEntry, LedgerEntry.transaction_id == LedgerTransaction.id)
            .group_by(LedgerTransaction.id)
            .having(
                (func.coalesce(func.sum(LedgerEntry.amount), 0) != 0)
                | (func.count(LedgerEntry.id) < 2)
            )
            .order_by(LedgerTransaction.id)
            .limit(_MAX_REPORTED)
        )
        active = await session.scalars(
            text("SELECT tgname FROM pg_trigger WHERE tgname = ANY(:names) AND tgenabled <> 'D'"),
            {"names": sorted(EXPECTED_TRIGGERS)},
        )
        present = set(active)

    return LedgerHealth(
        total=total,
        total_test=total_test,
        total_live=total_live,
        transactions_count=transactions_count,
        entries_count=entries_count,
        unbalanced_transactions=list(unbalanced),
        missing_triggers=sorted(EXPECTED_TRIGGERS - present),
    )
