"""Fenêtre glissante exacte : jamais plus de N requêtes sur la période (XG-13)."""

import asyncio
import uuid

from redis.asyncio import Redis

from xaalis.api.resources import Resources
from xaalis.infra.ratelimit import RateLimiter


async def test_never_more_than_limit_even_under_concurrency(resources: Resources) -> None:
    bucket = f"test:{uuid.uuid4()}"
    decisions = await asyncio.gather(
        *(resources.rate_limiter.hit(bucket, limit=10, period_seconds=60) for _ in range(30))
    )
    assert sum(d.allowed for d in decisions) == 10
    refused = [d for d in decisions if not d.allowed]
    assert all(1 <= d.retry_after_seconds <= 60 for d in refused)


async def test_window_slides(resources: Resources) -> None:
    bucket = f"test:{uuid.uuid4()}"
    assert (await resources.rate_limiter.hit(bucket, limit=1, period_seconds=1)).allowed
    assert not (await resources.rate_limiter.hit(bucket, limit=1, period_seconds=1)).allowed
    await asyncio.sleep(1.1)
    assert (await resources.rate_limiter.hit(bucket, limit=1, period_seconds=1)).allowed


async def test_fails_open_when_redis_is_unreachable() -> None:
    broken = Redis.from_url("redis://127.0.0.1:1/0", socket_connect_timeout=0.2)
    try:
        decision = await RateLimiter(broken).hit("any", limit=1, period_seconds=60)
    finally:
        await broken.aclose()
    assert decision.allowed  # fail-open : Redis en panne ne bloque pas les paiements
