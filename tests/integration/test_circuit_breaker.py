"""Disjoncteur par opérateur (lot 3) : fermé → ouvert (5 échecs) → semi-ouvert (après délai)."""

import asyncio
import uuid

import pytest

from xaalis.api.resources import Resources
from xaalis.core.operators import Operator
from xaalis.routing.health import BreakerState, OperatorHealth


@pytest.fixture
def health(resources: Resources) -> OperatorHealth:
    return OperatorHealth(
        resources.redis, failure_threshold=5, cooldown_seconds=1, namespace=f"t-{uuid.uuid4()}"
    )


async def test_breaker_opens_after_five_consecutive_failures(health: OperatorHealth) -> None:
    for _ in range(4):
        assert await health.record_failure(Operator.NDIAGA) is BreakerState.CLOSED
    assert await health.record_failure(Operator.NDIAGA) is BreakerState.OPEN
    assert not await health.allow(Operator.NDIAGA)
    assert await health.allow(Operator.GOPAY), "un disjoncteur par opérateur"


async def test_a_success_resets_the_consecutive_failure_count(health: OperatorHealth) -> None:
    for _ in range(4):
        await health.record_failure(Operator.GOPAY)
    await health.record_success(Operator.GOPAY)
    for _ in range(4):
        await health.record_failure(Operator.GOPAY)
    assert await health.state(Operator.GOPAY) is BreakerState.CLOSED


async def test_half_open_lets_exactly_one_probe_through(health: OperatorHealth) -> None:
    for _ in range(5):
        await health.record_failure(Operator.TERANGA)
    await asyncio.sleep(1.1)
    assert await health.state(Operator.TERANGA) is BreakerState.HALF_OPEN
    probes = await asyncio.gather(*(health.allow(Operator.TERANGA) for _ in range(10)))
    assert probes.count(True) == 1


async def test_successful_probe_closes_failed_probe_reopens(health: OperatorHealth) -> None:
    for _ in range(5):
        await health.record_failure(Operator.GOPAY)
    await asyncio.sleep(1.1)
    assert await health.allow(Operator.GOPAY)
    assert await health.record_failure(Operator.GOPAY) is BreakerState.OPEN

    await asyncio.sleep(1.1)
    assert await health.allow(Operator.GOPAY)
    await health.record_success(Operator.GOPAY)
    assert await health.state(Operator.GOPAY) is BreakerState.CLOSED
    assert await health.allow(Operator.GOPAY)


async def test_success_rate_over_last_five_minutes(health: OperatorHealth) -> None:
    assert await health.success_permille(Operator.GOPAY) is None
    for outcome in (True, True, True, False):
        await health.record_outcome(Operator.GOPAY, succeeded=outcome)
    assert await health.success_permille(Operator.GOPAY) == 750
    snapshot = await health.snapshot()
    assert snapshot.success_permille[Operator.GOPAY] == 750


async def test_snapshot_lists_open_breakers(health: OperatorHealth) -> None:
    for _ in range(5):
        await health.record_failure(Operator.NDIAGA)
    snapshot = await health.snapshot()
    assert snapshot.open_operators == frozenset({Operator.NDIAGA})
