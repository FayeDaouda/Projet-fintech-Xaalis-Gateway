"""Lecture du journal comptable : transactions et leurs écritures (lecture seule).

Sert le back-office (``GET /v1/admin/ledger/transactions``) : rendre visible la partie
double, transaction par transaction. Deux requêtes au total, quelle que soit la taille de
la page (transactions, puis toutes leurs écritures en une fois) : pas de N+1.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from xaalis.ledger.models import LedgerAccount, LedgerEntry, LedgerTransaction


@dataclass(frozen=True, slots=True)
class JournalLine:
    account: str
    account_kind: str
    amount: int


@dataclass(frozen=True, slots=True)
class JournalTransaction:
    transaction: LedgerTransaction
    lines: tuple[JournalLine, ...]

    @property
    def total(self) -> int:
        return sum(line.amount for line in self.lines)


async def list_transactions(
    session: AsyncSession,
    *,
    limit: int,
    starting_after: str | None = None,
    merchant_id: str | None = None,
    reference: str | None = None,
) -> tuple[list[JournalTransaction], bool]:
    """Transactions de la plus récente à la plus ancienne (identifiants ULID triables)."""
    query = select(LedgerTransaction)
    if starting_after is not None:
        query = query.where(LedgerTransaction.id < starting_after)
    if reference is not None:
        query = query.where(LedgerTransaction.reference == reference)
    if merchant_id is not None:
        touches_merchant = (
            select(LedgerEntry.id)
            .join(
                LedgerAccount,
                (LedgerAccount.id == LedgerEntry.account_id)
                & (LedgerAccount.livemode == LedgerEntry.livemode),
            )
            .where(
                LedgerEntry.transaction_id == LedgerTransaction.id,
                LedgerAccount.merchant_id == merchant_id,
            )
        )
        query = query.where(exists(touches_merchant))
    rows: Sequence[LedgerTransaction] = (
        await session.scalars(query.order_by(LedgerTransaction.id.desc()).limit(limit + 1))
    ).all()
    page = list(rows[:limit])
    lines = await _lines_of([row.id for row in page], session)
    return [JournalTransaction(row, tuple(lines[row.id])) for row in page], len(rows) > limit


async def _lines_of(
    transaction_ids: list[str], session: AsyncSession
) -> dict[str, list[JournalLine]]:
    grouped: dict[str, list[JournalLine]] = defaultdict(list)
    if not transaction_ids:
        return grouped
    result = await session.execute(
        select(
            LedgerEntry.transaction_id, LedgerAccount.code, LedgerAccount.kind, LedgerEntry.amount
        )
        .join(
            LedgerAccount,
            (LedgerAccount.id == LedgerEntry.account_id)
            & (LedgerAccount.livemode == LedgerEntry.livemode),
        )
        .where(LedgerEntry.transaction_id.in_(transaction_ids))
        .order_by(LedgerEntry.transaction_id, LedgerEntry.amount.desc(), LedgerEntry.id)
    )
    for transaction_id, code, kind, amount in result:
        grouped[transaction_id].append(JournalLine(code, kind, amount))
    return grouped
