"""Propriété Hypothesis : après TOUTE séquence d'opérations, le grand livre est équilibré
(XG-16) et les soldes du marchand égalent ceux du modèle de référence."""

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.integration.ledger_simulation import (
    MerchantModel,
    Operation,
    apply_operation,
    operations,
)
from tests.support import MerchantFactory
from xaalis.ledger import service
from xaalis.ledger.health import check_ledger_health


@settings(
    max_examples=40,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
@given(sequence=st.lists(operations, min_size=1, max_size=15))
async def test_xg16_property_ledger_stays_balanced_after_any_sequence(
    sessionmaker: async_sessionmaker[AsyncSession],
    make_merchant: MerchantFactory,
    sequence: list[Operation],
) -> None:
    model = MerchantModel(await make_merchant())
    for op in sequence:
        await apply_operation(sessionmaker, model, op)

    async with sessionmaker() as session:
        actual = await service.merchant_balances(session, model.merchant_id, livemode=False)
    assert actual == model.expected()

    async with sessionmaker() as session:
        health = await check_ledger_health(session)
    assert health.balanced, health.unbalanced_transactions
