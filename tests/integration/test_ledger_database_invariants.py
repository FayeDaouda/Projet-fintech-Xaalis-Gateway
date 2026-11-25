"""XG-16 garanti par PostgreSQL lui-même, indépendamment du code applicatif.

Chaque test contourne volontairement le module ``xaalis.ledger`` et écrit en SQL brut,
comme le ferait un bogue ou un accès direct à la base : la base doit refuser.
"""

from collections.abc import Mapping, Sequence

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.support import MerchantFactory, sqlstate
from xaalis.core.fees import fee_breakdown
from xaalis.core.ids import new_id
from xaalis.core.operators import Operator
from xaalis.ledger import recipes, service
from xaalis.ledger.lines import TransactionKind

XG016 = "XG016"
CHECK_VIOLATION = "23514"
FOREIGN_KEY_VIOLATION = "23503"

Statement = tuple[str, Mapping[str, object]]

INSERT_HEADER = (
    "INSERT INTO ledger_transactions (id, kind, reference, livemode) "
    "VALUES (:id, 'settlement', :ref, :lm)"
)
INSERT_PAIR = (
    "INSERT INTO ledger_entries (transaction_id, account_id, livemode, amount) "
    "VALUES (:tx, :acc, :lm, :debit), (:tx, :acc, :lm, :credit)"
)


async def _execute_in_transaction(
    sessionmaker: async_sessionmaker[AsyncSession], statements: Sequence[Statement]
) -> None:
    """Exécute les instructions puis valide : les triggers différés s'exécutent au COMMIT."""
    async with sessionmaker.begin() as session:
        for sql, params in statements:
            await session.execute(text(sql), params)


def _header(tx_id: str, *, livemode: bool = False) -> Statement:
    return INSERT_HEADER, {"id": tx_id, "ref": new_id("pi"), "lm": livemode}


def _pair(
    tx_id: str, account: int, debit: int, credit: int, *, livemode: bool = False
) -> Statement:
    return INSERT_PAIR, {
        "tx": tx_id,
        "acc": account,
        "lm": livemode,
        "debit": debit,
        "credit": credit,
    }


@pytest.fixture
async def committed_capture(
    sessionmaker: async_sessionmaker[AsyncSession], make_merchant: MerchantFactory
) -> str:
    merchant_id = await make_merchant()
    async with sessionmaker.begin() as session:
        result = await service.post(
            session,
            livemode=False,
            kind=TransactionKind.PAYMENT_CAPTURE,
            reference=new_id("pi"),
            lines=recipes.payment_capture(merchant_id, Operator.GOPAY, fee_breakdown(10_000)),
        )
    return result.transaction_id


@pytest.fixture
async def clearing_account(
    sessionmaker: async_sessionmaker[AsyncSession], committed_capture: str
) -> int:
    """Identifiant du compte de test operator_clearing:gopay (créé par la capture)."""
    async with sessionmaker() as session:
        value = await session.scalar(
            text("SELECT id FROM ledger_accounts WHERE code = :c AND NOT livemode"),
            {"c": "operator_clearing:gopay"},
        )
    assert isinstance(value, int)
    return value


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE ledger_entries SET amount = amount * 2 WHERE transaction_id = :tx",
        "DELETE FROM ledger_entries WHERE transaction_id = :tx",
        "UPDATE ledger_transactions SET reference = 'pi_forged' WHERE id = :tx",
        "DELETE FROM ledger_transactions WHERE id = :tx",
        "UPDATE ledger_accounts SET code = 'forged'",
    ],
    ids=[
        "update-entry",
        "delete-entry",
        "update-transaction",
        "delete-transaction",
        "update-account",
    ],
)
async def test_xg16_committed_ledger_rows_are_immutable(
    sessionmaker: async_sessionmaker[AsyncSession], committed_capture: str, statement: str
) -> None:
    with pytest.raises(DBAPIError) as excinfo:
        await _execute_in_transaction(sessionmaker, [(statement, {"tx": committed_capture})])
    assert sqlstate(excinfo.value) == XG016


@pytest.mark.parametrize("table", ["ledger_entries", "ledger_transactions", "ledger_accounts"])
async def test_xg16_ledger_tables_cannot_be_truncated(
    sessionmaker: async_sessionmaker[AsyncSession], table: str
) -> None:
    with pytest.raises(DBAPIError) as excinfo:
        await _execute_in_transaction(sessionmaker, [(f"TRUNCATE {table} CASCADE", {})])
    assert sqlstate(excinfo.value) == XG016


async def test_xg16_unbalanced_transaction_is_rejected_at_commit(
    sessionmaker: async_sessionmaker[AsyncSession], clearing_account: int
) -> None:
    tx_id = new_id("ltx")
    with pytest.raises(DBAPIError) as excinfo:
        await _execute_in_transaction(
            sessionmaker, [_header(tx_id), _pair(tx_id, clearing_account, 5_000, -4_999)]
        )
    assert sqlstate(excinfo.value) == XG016

    async with sessionmaker() as session:
        remaining = await session.scalar(
            text("SELECT count(*) FROM ledger_transactions WHERE id = :id"), {"id": tx_id}
        )
    assert remaining == 0, "la transaction SQL entière doit avoir été annulée"


async def test_xg16_transaction_without_entries_is_rejected_at_commit(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    with pytest.raises(DBAPIError) as excinfo:
        await _execute_in_transaction(sessionmaker, [_header(new_id("ltx"))])
    assert sqlstate(excinfo.value) == XG016


async def test_xg16_committed_transaction_is_sealed_against_new_entries(
    sessionmaker: async_sessionmaker[AsyncSession], committed_capture: str, clearing_account: int
) -> None:
    """Même une paire équilibrée ne peut pas être ajoutée à une transaction déjà validée."""
    with pytest.raises(DBAPIError) as excinfo:
        await _execute_in_transaction(
            sessionmaker, [_pair(committed_capture, clearing_account, 100, -100)]
        )
    assert sqlstate(excinfo.value) == XG016


async def test_xg16_zero_amount_entry_is_rejected(
    sessionmaker: async_sessionmaker[AsyncSession], clearing_account: int
) -> None:
    tx_id = new_id("ltx")
    with pytest.raises(DBAPIError) as excinfo:
        await _execute_in_transaction(
            sessionmaker, [_header(tx_id), _pair(tx_id, clearing_account, 0, 0)]
        )
    assert sqlstate(excinfo.value) == CHECK_VIOLATION


async def test_xg12_entry_cannot_mix_test_and_live(
    sessionmaker: async_sessionmaker[AsyncSession], clearing_account: int
) -> None:
    """Une écriture live sur un compte de test viole la clé étrangère composite."""
    tx_id = new_id("ltx")
    with pytest.raises(DBAPIError) as excinfo:
        await _execute_in_transaction(
            sessionmaker,
            [
                _header(tx_id, livemode=True),
                _pair(tx_id, clearing_account, 100, -100, livemode=True),
            ],
        )
    assert sqlstate(excinfo.value) == FOREIGN_KEY_VIOLATION
