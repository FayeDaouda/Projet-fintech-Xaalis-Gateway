"""Service du grand livre : comptabilisation, idempotence, extourne, soldes."""

import asyncio

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.support import MerchantFactory
from xaalis.core.fees import fee_breakdown
from xaalis.core.ids import new_id
from xaalis.core.operators import Operator
from xaalis.ledger import recipes, service
from xaalis.ledger.accounts import AccountRef
from xaalis.ledger.lines import TransactionKind
from xaalis.ledger.service import LedgerTransactionNotFoundError, MerchantBalances


async def _capture(
    sessionmaker: async_sessionmaker[AsyncSession],
    merchant_id: str,
    amount: int,
    *,
    reference: str | None = None,
    livemode: bool = False,
    operator: Operator = Operator.GOPAY,
) -> service.PostingResult:
    async with sessionmaker.begin() as session:
        return await service.post(
            session,
            livemode=livemode,
            kind=TransactionKind.PAYMENT_CAPTURE,
            reference=reference or new_id("pi"),
            lines=recipes.payment_capture(merchant_id, operator, fee_breakdown(amount)),
        )


async def _balances(
    sessionmaker: async_sessionmaker[AsyncSession], merchant_id: str, *, livemode: bool = False
) -> MerchantBalances:
    async with sessionmaker() as session:
        return await service.merchant_balances(session, merchant_id, livemode=livemode)


async def test_xg16_capture_of_10000_matches_specification_example(
    sessionmaker: async_sessionmaker[AsyncSession], make_merchant: MerchantFactory
) -> None:
    merchant_id = await make_merchant()
    async with sessionmaker.begin() as session:
        clearing_before = await service.account_balance(
            session, AccountRef.operator_clearing(Operator.TERANGA), livemode=False
        )
    await _capture(sessionmaker, merchant_id, 10_000, operator=Operator.TERANGA)

    async with sessionmaker() as session:
        pending = await service.account_balance(
            session, AccountRef.merchant_pending(merchant_id), livemode=False
        )
        clearing = await service.account_balance(
            session, AccountRef.operator_clearing(Operator.TERANGA), livemode=False
        )
    assert pending == 9_820
    assert clearing - clearing_before == 10_000


async def test_posting_the_same_fact_twice_is_idempotent(
    sessionmaker: async_sessionmaker[AsyncSession], make_merchant: MerchantFactory
) -> None:
    merchant_id = await make_merchant()
    reference = new_id("pi")
    first = await _capture(sessionmaker, merchant_id, 10_000, reference=reference)
    replay = await _capture(sessionmaker, merchant_id, 10_000, reference=reference)

    assert first.created
    assert not replay.created
    assert replay.transaction_id == first.transaction_id
    assert (await _balances(sessionmaker, merchant_id)).pending == 9_820


async def test_concurrent_postings_of_the_same_fact_produce_a_single_transaction(
    sessionmaker: async_sessionmaker[AsyncSession], make_merchant: MerchantFactory
) -> None:
    """Simule un callback opérateur reçu 20 fois en même temps (cf. Teranga Money)."""
    merchant_id = await make_merchant()
    reference = new_id("pi")

    results = await asyncio.gather(
        *(_capture(sessionmaker, merchant_id, 10_000, reference=reference) for _ in range(20))
    )

    assert sum(result.created for result in results) == 1
    assert len({result.transaction_id for result in results}) == 1
    assert (await _balances(sessionmaker, merchant_id)).pending == 9_820


async def test_reversal_restores_every_balance(
    sessionmaker: async_sessionmaker[AsyncSession], make_merchant: MerchantFactory
) -> None:
    merchant_id = await make_merchant()
    capture = await _capture(sessionmaker, merchant_id, 25_000)

    async with sessionmaker.begin() as session:
        reversal = await service.reverse(session, capture.transaction_id, reason="erreur de test")
    assert reversal.created
    assert await _balances(sessionmaker, merchant_id) == MerchantBalances(0, 0, 0)


async def test_reversing_twice_is_idempotent(
    sessionmaker: async_sessionmaker[AsyncSession], make_merchant: MerchantFactory
) -> None:
    merchant_id = await make_merchant()
    capture = await _capture(sessionmaker, merchant_id, 25_000)
    async with sessionmaker.begin() as session:
        first = await service.reverse(session, capture.transaction_id, reason="doublon")
    async with sessionmaker.begin() as session:
        second = await service.reverse(session, capture.transaction_id, reason="doublon")
    assert not second.created
    assert second.transaction_id == first.transaction_id
    assert (await _balances(sessionmaker, merchant_id)).pending == 0


async def test_reversing_an_unknown_transaction_fails(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with sessionmaker() as session:
        with pytest.raises(LedgerTransactionNotFoundError):
            await service.reverse(session, "ltx_UNKNOWN", reason="n/a")


async def test_settlement_then_payout_move_funds_through_merchant_accounts(
    sessionmaker: async_sessionmaker[AsyncSession], make_merchant: MerchantFactory
) -> None:
    merchant_id = await make_merchant()
    capture_ref = new_id("pi")
    await _capture(sessionmaker, merchant_id, 10_000, reference=capture_ref)

    async with sessionmaker.begin() as session:
        await service.post(
            session,
            livemode=False,
            kind=TransactionKind.SETTLEMENT,
            reference=capture_ref,
            lines=recipes.settlement(merchant_id, 9_820),
        )
    assert await _balances(sessionmaker, merchant_id) == MerchantBalances(0, 9_820, 0)

    async with sessionmaker.begin() as session:
        await service.post(
            session,
            livemode=False,
            kind=TransactionKind.PAYOUT,
            reference=new_id("po"),
            lines=recipes.payout(merchant_id, 9_820),
        )
    assert await _balances(sessionmaker, merchant_id) == MerchantBalances(0, 0, 9_820)


async def test_xg12_live_postings_never_affect_test_balances(
    sessionmaker: async_sessionmaker[AsyncSession], make_merchant: MerchantFactory
) -> None:
    merchant_id = await make_merchant()
    await _capture(sessionmaker, merchant_id, 10_000, livemode=True)

    assert (await _balances(sessionmaker, merchant_id, livemode=True)).pending == 9_820
    assert (await _balances(sessionmaker, merchant_id, livemode=False)).pending == 0
