"""Écriture dans le grand livre : comptabilisation, extourne, soldes.

Ces fonctions ne valident jamais (``commit``) : elles s'exécutent dans la transaction
du cas d'usage appelant, pour que l'état métier, le grand livre et l'outbox soient
écrits atomiquement (ADR 0002, ADR 0003).
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass

from sqlalchemy import BigInteger, cast, func, insert, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from xaalis.core.ids import new_id
from xaalis.core.operators import Operator
from xaalis.ledger.accounts import AccountKind, AccountRef
from xaalis.ledger.lines import Line, TransactionKind, validate_lines
from xaalis.ledger.models import LedgerAccount, LedgerEntry, LedgerTransaction


class LedgerTransactionNotFoundError(LookupError):
    pass


@dataclass(frozen=True, slots=True)
class PostingResult:
    transaction_id: str
    created: bool  # False : ce fait métier était déjà comptabilisé (rejeu idempotent)


async def post(
    session: AsyncSession,
    *,
    livemode: bool,
    kind: TransactionKind,
    reference: str,
    lines: Sequence[Line],
    description: str | None = None,
    reverses_id: str | None = None,
) -> PostingResult:
    """Comptabilise un fait métier, au plus une fois par couple ``(kind, reference)``.

    En cas d'appel concurrent pour le même fait, l'``INSERT … ON CONFLICT DO NOTHING``
    du second attend la fin du premier : s'il a validé, on renvoie sa transaction.
    """
    validate_lines(lines)

    transaction_id = await session.scalar(
        pg_insert(LedgerTransaction)
        .values(
            id=new_id("ltx"),
            kind=kind.value,
            reference=reference,
            livemode=livemode,
            description=description,
            reverses_id=reverses_id,
        )
        .on_conflict_do_nothing(index_elements=["kind", "reference"])
        .returning(LedgerTransaction.id)
    )
    if transaction_id is None:
        existing = await session.scalar(
            select(LedgerTransaction.id).where(
                LedgerTransaction.kind == kind.value, LedgerTransaction.reference == reference
            )
        )
        if existing is None:  # pragma: no cover — impossible : le conflit implique une ligne
            msg = f"conflit sans transaction existante pour {kind}:{reference}"
            raise RuntimeError(msg)
        return PostingResult(transaction_id=existing, created=False)

    account_ids = await _ensure_accounts(session, livemode, {line.account for line in lines})
    await session.execute(
        insert(LedgerEntry),
        [
            {
                "transaction_id": transaction_id,
                "account_id": account_ids[line.account.code],
                "livemode": livemode,
                "amount": line.amount,
            }
            for line in lines
        ],
    )
    return PostingResult(transaction_id=transaction_id, created=True)


async def reverse(session: AsyncSession, transaction_id: str, *, reason: str) -> PostingResult:
    """Extourne : nouvelle transaction aux montants opposés (on ne modifie jamais l'historique)."""
    original = await session.get(LedgerTransaction, transaction_id)
    if original is None:
        raise LedgerTransactionNotFoundError(transaction_id)

    rows = await session.execute(
        select(
            LedgerEntry.amount,
            LedgerAccount.kind,
            LedgerAccount.merchant_id,
            LedgerAccount.operator,
        )
        .join(LedgerAccount, LedgerAccount.id == LedgerEntry.account_id)
        .where(LedgerEntry.transaction_id == transaction_id)
        .order_by(LedgerEntry.id)
    )
    lines = [
        Line(
            AccountRef(
                AccountKind(kind),
                merchant_id=merchant_id,
                operator=Operator(operator) if operator else None,
            ),
            -amount,
        )
        for amount, kind, merchant_id, operator in rows
    ]
    return await post(
        session,
        livemode=original.livemode,
        kind=TransactionKind.REVERSAL,
        reference=original.id,
        reverses_id=original.id,
        lines=lines,
        description=reason[:255],
    )


async def _ensure_accounts(
    session: AsyncSession, livemode: bool, accounts: Collection[AccountRef]
) -> dict[str, int]:
    """Crée les comptes manquants (idempotent) et renvoie leurs identifiants par code.

    Les comptes sont insérés dans un ordre déterministe (par code) : deux transactions
    concurrentes qui créent les mêmes comptes ne peuvent pas s'interbloquer.
    """
    ordered = sorted(accounts, key=lambda ref: ref.code)
    await session.execute(
        pg_insert(LedgerAccount)
        .values(
            [
                {
                    "code": ref.code,
                    "kind": ref.kind.value,
                    "livemode": livemode,
                    "merchant_id": ref.merchant_id,
                    "operator": ref.operator.value if ref.operator else None,
                }
                for ref in ordered
            ]
        )
        .on_conflict_do_nothing(index_elements=["code", "livemode"])
    )
    rows = await session.execute(
        select(LedgerAccount.code, LedgerAccount.id).where(
            LedgerAccount.livemode == livemode,
            LedgerAccount.code.in_([ref.code for ref in ordered]),
        )
    )
    return dict(rows.all())


# --------------------------------------------------------------------------- soldes


# SUM(bigint) renvoie un numeric (Decimal côté Python) : on le convertit en BIGINT
# dans la requête pour rester en entiers de bout en bout (XG-01).


async def account_balance(session: AsyncSession, account: AccountRef, *, livemode: bool) -> int:
    """Solde d'un compte dans son sens normal (positif = ce que le compte « contient »)."""
    total = await session.scalar(
        select(cast(func.coalesce(func.sum(LedgerEntry.amount), 0), BigInteger))
        .join(LedgerAccount, LedgerAccount.id == LedgerEntry.account_id)
        .where(LedgerAccount.code == account.code, LedgerAccount.livemode == livemode)
    )
    return int(total or 0) * account.kind.normal_sign


@dataclass(frozen=True, slots=True)
class MerchantBalances:
    pending: int
    available: int
    paid_out: int


async def merchant_balances(
    session: AsyncSession, merchant_id: str, *, livemode: bool
) -> MerchantBalances:
    rows = await session.execute(
        select(
            LedgerAccount.kind,
            cast(func.coalesce(func.sum(LedgerEntry.amount), 0), BigInteger),
        )
        .outerjoin(LedgerEntry, LedgerEntry.account_id == LedgerAccount.id)
        .where(LedgerAccount.merchant_id == merchant_id, LedgerAccount.livemode == livemode)
        .group_by(LedgerAccount.kind)
    )
    by_kind: dict[AccountKind, int] = {}
    for raw_kind, total in rows:
        kind = AccountKind(raw_kind)
        by_kind[kind] = int(total) * kind.normal_sign
    return MerchantBalances(
        pending=by_kind.get(AccountKind.MERCHANT_PENDING, 0),
        available=by_kind.get(AccountKind.MERCHANT_AVAILABLE, 0),
        paid_out=by_kind.get(AccountKind.MERCHANT_PAYOUT, 0),
    )
