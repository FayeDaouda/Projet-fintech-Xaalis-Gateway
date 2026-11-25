"""Santé des opérateurs, partagée via Redis : disjoncteur et taux de succès récent (lot 3).

Disjoncteur par opérateur ::

    fermé ──5 échecs consécutifs──▶ ouvert ──30 s──▶ semi-ouvert ──succès──▶ fermé
                                       ▲                  │
                                       └────── échec ─────┘

En semi-ouvert, **un seul** appel d'essai est autorisé à la fois (verrou ``probe_until``) :
on ne submerge pas un opérateur qui se relève. Chaque opération est un script Lua,
donc atomique même avec plusieurs workers ; l'horloge est celle de Redis.

Taux de succès : compteurs par minute (succès, total) conservés 6 minutes ; le taux sur
les 5 dernières minutes est exprimé en pour mille (entier, XG-01).
"""

from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum
from typing import Final

from redis.asyncio import Redis
from redis.exceptions import RedisError

from xaalis.core.operators import Operator
from xaalis.observability.logging import get_logger
from xaalis.routing.catalog import RoutingSnapshot

_log = get_logger(__name__)


class BreakerState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


_NOW = """
local t = redis.call('TIME')
local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
"""

# ARGV[1] = délai de refroidissement (ms). Renvoie {autorisé (0/1), état}.
_ALLOW_LUA: Final = (
    _NOW
    + """
local cooldown = tonumber(ARGV[1])
local state = redis.call('HGET', KEYS[1], 'state') or 'closed'
if state == 'open' then
  local opened = tonumber(redis.call('HGET', KEYS[1], 'opened_at') or '0')
  if now - opened < cooldown then return {0, 'open'} end
  state = 'half_open'
  redis.call('HSET', KEYS[1], 'state', 'half_open')
end
if state == 'half_open' then
  local probe = tonumber(redis.call('HGET', KEYS[1], 'probe_until') or '0')
  if probe > now then return {0, 'half_open'} end
  redis.call('HSET', KEYS[1], 'probe_until', now + cooldown)
  return {1, 'half_open'}
end
return {1, 'closed'}
"""
)

_SUCCESS_LUA: Final = """
redis.call('HSET', KEYS[1], 'state', 'closed', 'failures', 0)
redis.call('HDEL', KEYS[1], 'opened_at', 'probe_until')
return 'closed'
"""

# ARGV[1] = seuil d'échecs consécutifs. Renvoie le nouvel état.
_FAILURE_LUA: Final = (
    _NOW
    + """
local threshold = tonumber(ARGV[1])
local state = redis.call('HGET', KEYS[1], 'state') or 'closed'
local failures = redis.call('HINCRBY', KEYS[1], 'failures', 1)
if state == 'half_open' or failures >= threshold then
  redis.call('HSET', KEYS[1], 'state', 'open', 'opened_at', now)
  redis.call('HDEL', KEYS[1], 'probe_until')
  return 'open'
end
return state
"""
)

# Lecture sans effet de bord : un disjoncteur ouvert dont le délai est écoulé est
# présenté « semi-ouvert ».
_STATE_LUA: Final = (
    _NOW
    + """
local cooldown = tonumber(ARGV[1])
local state = redis.call('HGET', KEYS[1], 'state') or 'closed'
if state == 'open' then
  local opened = tonumber(redis.call('HGET', KEYS[1], 'opened_at') or '0')
  if now - opened >= cooldown then return 'half_open' end
end
return state
"""
)

_RATE_WINDOW_MINUTES: Final = 5


class OperatorHealth:
    def __init__(
        self,
        redis: Redis,
        *,
        failure_threshold: int,
        cooldown_seconds: int,
        namespace: str = "xaalis",
    ) -> None:
        self._redis = redis
        self._threshold = failure_threshold
        self._cooldown_ms = cooldown_seconds * 1000
        self._ns = namespace
        self._allow = redis.register_script(_ALLOW_LUA)
        self._success = redis.register_script(_SUCCESS_LUA)
        self._failure = redis.register_script(_FAILURE_LUA)
        self._state = redis.register_script(_STATE_LUA)

    def _breaker_key(self, operator: Operator) -> str:
        return f"{self._ns}:cb:{operator.value}"

    # ------------------------------------------------------------------ disjoncteur

    async def allow(self, operator: Operator) -> bool:
        """Peut-on appeler cet opérateur maintenant ? (fail-open si Redis est en panne)"""
        try:
            allowed, _ = await self._allow(
                keys=[self._breaker_key(operator)], args=[self._cooldown_ms]
            )
        except RedisError as exc:
            _log.warning("breaker_unavailable", operator=operator.value, error=type(exc).__name__)
            return True
        return int(allowed) == 1

    async def record_success(self, operator: Operator) -> None:
        try:
            await self._success(keys=[self._breaker_key(operator)])
        except RedisError as exc:  # pragma: no cover — journalisé, sans incidence métier
            _log.warning("breaker_unavailable", operator=operator.value, error=type(exc).__name__)

    async def record_failure(self, operator: Operator) -> BreakerState:
        try:
            state = await self._failure(keys=[self._breaker_key(operator)], args=[self._threshold])
        except RedisError as exc:  # pragma: no cover
            _log.warning("breaker_unavailable", operator=operator.value, error=type(exc).__name__)
            return BreakerState.CLOSED
        new_state = BreakerState(state if isinstance(state, str) else state.decode())
        if new_state is BreakerState.OPEN:
            _log.warning("breaker_opened", operator=operator.value)
        return new_state

    async def state(self, operator: Operator) -> BreakerState:
        try:
            state = await self._state(keys=[self._breaker_key(operator)], args=[self._cooldown_ms])
        except RedisError:
            return BreakerState.CLOSED
        return BreakerState(state if isinstance(state, str) else state.decode())

    async def reset(self, operator: Operator) -> None:
        await self._redis.delete(self._breaker_key(operator))

    # ------------------------------------------------------------- taux de succès

    def _rate_keys(self, operator: Operator, minute: int) -> tuple[str, str]:
        base = f"{self._ns}:sr:{operator.value}:{minute}"
        return f"{base}:ok", f"{base}:all"

    async def _minute(self) -> int:
        seconds, _ = await self._redis.time()
        return int(seconds) // 60

    async def record_outcome(self, operator: Operator, *, succeeded: bool) -> None:
        """Issue finale d'un paiement (callback) : alimente le taux de succès sur 5 min."""
        try:
            ok_key, all_key = self._rate_keys(operator, await self._minute())
            async with self._redis.pipeline(transaction=True) as pipe:
                if succeeded:
                    pipe.incr(ok_key)
                    pipe.expire(ok_key, 360)
                pipe.incr(all_key)
                pipe.expire(all_key, 360)
                await pipe.execute()
        except RedisError as exc:  # pragma: no cover
            _log.warning("success_rate_unavailable", error=type(exc).__name__)

    async def success_permille(self, operator: Operator) -> int | None:
        """Taux de succès des 5 dernières minutes en ‰ ; ``None`` sans donnée."""
        minute = await self._minute()
        keys = [
            key
            for m in range(minute - _RATE_WINDOW_MINUTES + 1, minute + 1)
            for key in self._rate_keys(operator, m)
        ]
        values = [int(v or 0) for v in await self._redis.mget(keys)]
        succeeded, total = sum(values[0::2]), sum(values[1::2])
        return None if total == 0 else succeeded * 1_000 // total

    async def snapshot(self, operators: Iterable[Operator] = tuple(Operator)) -> RoutingSnapshot:
        try:
            open_ops = frozenset(
                [op for op in operators if await self.state(op) is BreakerState.OPEN]
            )
            rates = {}
            for op in Operator:
                rate = await self.success_permille(op)
                if rate is not None:
                    rates[op] = rate
        except RedisError as exc:
            _log.warning("routing_snapshot_unavailable", error=type(exc).__name__)
            return RoutingSnapshot()
        return RoutingSnapshot(open_operators=open_ops, success_permille=rates)
