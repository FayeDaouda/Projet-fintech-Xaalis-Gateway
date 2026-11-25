"""Limitation de débit distribuée (XG-13) : fenêtre glissante exacte dans Redis.

Garantie : **jamais plus de ``limit`` requêtes sur n'importe quelles ``period`` secondes**.
Chaque compteur est un *sorted set* des horodatages acceptés ; un script Lua, exécuté
atomiquement, purge ceux qui sortent de la fenêtre, compte, puis accepte ou refuse.

Pourquoi pas GCRA ou un seau à jetons ? Ils rechargent en continu : sur 60 s glissantes,
ils laissent passer jusqu'à ~2 × ``limit`` requêtes (rafale + recharge). Une fenêtre
fixe a le même défaut à la frontière de deux minutes. La fenêtre glissante coûte au plus
``limit`` entrées par compteur (100), ce qui reste négligeable (ADR 0006).

L'horloge est celle de Redis (``TIME``), commune à toutes les instances de l'API.
En cas de panne de Redis, on laisse passer (*fail-open*) en le journalisant.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Final

from redis.asyncio import Redis
from redis.exceptions import RedisError

from xaalis.observability.logging import get_logger

_log = get_logger(__name__)

# KEYS[1] = compteur ; ARGV[1] = limite ; ARGV[2] = période (ms) ; ARGV[3] = identifiant unique.
# Renvoie {1, 0} si autorisé, {0, attente_ms} sinon.
_SLIDING_WINDOW_LUA: Final = """
local limit = tonumber(ARGV[1])
local period = tonumber(ARGV[2])
local t = redis.call('TIME')
local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now - period)
if redis.call('ZCARD', KEYS[1]) >= limit then
  local oldest = redis.call('ZRANGE', KEYS[1], 0, 0, 'WITHSCORES')
  return {0, tonumber(oldest[2]) + period - now}
end
redis.call('ZADD', KEYS[1], now, ARGV[3])
redis.call('PEXPIRE', KEYS[1], period)
return {1, 0}
"""


@dataclass(frozen=True, slots=True)
class RateDecision:
    allowed: bool
    retry_after_seconds: int


class RateLimiter:
    def __init__(self, redis: Redis, *, namespace: str = "xaalis:rl") -> None:
        self._script = redis.register_script(_SLIDING_WINDOW_LUA)
        self._namespace = namespace

    async def hit(self, bucket: str, *, limit: int, period_seconds: int) -> RateDecision:
        try:
            allowed, wait_ms = await self._script(
                keys=[f"{self._namespace}:{bucket}"],
                args=[limit, period_seconds * 1000, uuid.uuid4().hex],
            )
        except RedisError as exc:
            _log.warning("rate_limiter_unavailable", error=type(exc).__name__, bucket=bucket)
            return RateDecision(allowed=True, retry_after_seconds=0)
        if int(allowed) == 1:
            return RateDecision(allowed=True, retry_after_seconds=0)
        # Retry-After en secondes entières, arrondi au supérieur, au moins 1.
        return RateDecision(allowed=False, retry_after_seconds=max(1, -(-int(wait_ms) // 1000)))
