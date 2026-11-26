"""Livraison des webhooks : signature, relances avec backoff et jitter, dead letter.

- Signature : ``X-Xaalis-Signature: t=<ts>,v1=<HMAC-SHA-256(secret, f"{ts}.{corps}")>``,
  secret propre à chaque endpoint ; ``X-Xaalis-Event-Id`` porte l'``evt_…`` unique.
- Relances : 8 tentatives au total sur ≈ 24 h (délais configurables, ±20 % de jitter pour
  ne pas réveiller tous les endpoints en même temps), puis ``dead_letter``.
- Le corps est reconstruit de façon déterministe à chaque tentative : il est identique
  octet pour octet d'une tentative à l'autre.
- Les redirections ne sont pas suivies (une redirection pourrait viser le réseau interne).
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Final

import httpx
import structlog
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from xaalis.core.signatures import sign
from xaalis.observability.logging import get_logger
from xaalis.webhooks import crypto
from xaalis.webhooks.models import WebhookDelivery, WebhookEndpoint, WebhookEvent
from xaalis.webhooks.ssrf import (
    Resolver,
    UnsafeWebhookUrlError,
    assert_public_destination,
    system_resolver,
)

_log = get_logger(__name__)
USER_AGENT: Final = "Xaalis-Webhooks/1.0"
_LEASE: Final = timedelta(seconds=60)


def event_body(event: WebhookEvent) -> bytes:
    document = {
        "id": event.id,
        "object": "event",
        "type": event.type,
        "livemode": event.livemode,
        "created_at": event.created_at.isoformat().replace("+00:00", "Z"),
        "data": {"object": event.payload},
    }
    return json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def next_delay_seconds(schedule: Sequence[int], attempts: int, rng: random.Random) -> int | None:
    """Délai avant la tentative suivante, ``None`` s'il n'y en a plus (dead letter)."""
    if attempts > len(schedule):
        return None
    base = schedule[attempts - 1]
    return max(1, base * rng.randint(800, 1_200) // 1_000)  # jitter ±20 %, en entiers


@dataclass(frozen=True, slots=True)
class _Job:
    delivery_id: int
    attempts: int
    event: WebhookEvent
    endpoint: WebhookEndpoint


class WebhookDeliverer:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        http: httpx.AsyncClient,
        *,
        encryption_key: str,
        schedule: Sequence[int],
        resolver: Resolver = system_resolver,
        rng: random.Random | None = None,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._http = http
        self._key = encryption_key
        self._schedule = list(schedule)
        self._resolver = resolver
        self._rng = rng or random.Random()  # noqa: S311 — jitter, pas de cryptographie

    @property
    def max_attempts(self) -> int:
        return len(self._schedule) + 1

    async def run_batch(self, *, limit: int = 50) -> int:
        jobs = await self._claim(limit)
        await asyncio.gather(*(self._deliver(job) for job in jobs))
        return len(jobs)

    async def _claim(self, limit: int) -> list[_Job]:
        async with self._sessionmaker.begin() as session:
            due = (
                select(WebhookDelivery.id)
                .where(
                    WebhookDelivery.status == "pending",
                    WebhookDelivery.next_attempt_at <= func.now(),
                    (WebhookDelivery.locked_until.is_(None))
                    | (WebhookDelivery.locked_until < func.now()),
                )
                .order_by(WebhookDelivery.next_attempt_at)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
            claimed = (
                await session.execute(
                    update(WebhookDelivery)
                    .where(WebhookDelivery.id.in_(due.scalar_subquery()))
                    .values(
                        locked_until=func.now() + _LEASE,
                        attempts=WebhookDelivery.attempts + 1,
                    )
                    .returning(
                        WebhookDelivery.id,
                        WebhookDelivery.attempts,
                        WebhookDelivery.event_id,
                        WebhookDelivery.endpoint_id,
                    )
                )
            ).all()
            jobs = []
            for delivery_id, attempts, event_id, endpoint_id in claimed:
                event = await session.get(WebhookEvent, event_id)
                endpoint = await session.get(WebhookEndpoint, endpoint_id)
                if event is not None and endpoint is not None:
                    jobs.append(_Job(delivery_id, attempts, event, endpoint))
            return jobs

    async def _deliver(self, job: _Job) -> None:
        structlog.contextvars.bind_contextvars(
            request_id=job.event.request_id, event_id=job.event.id, endpoint_id=job.endpoint.id
        )
        body = event_body(job.event)
        timestamp = int(time.time())
        secret = crypto.decrypt(self._key, job.endpoint.secret_ciphertext)
        headers = {
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
            "X-Xaalis-Event-Id": job.event.id,
            "X-Xaalis-Event-Type": job.event.type,
            "X-Xaalis-Signature": f"t={timestamp},v1={sign(secret, timestamp, body)}",
        }
        if job.event.request_id:
            headers["X-Request-Id"] = job.event.request_id
        status_code: int | None = None
        try:
            if job.endpoint.livemode:  # SSRF : adresses réellement résolues, à chaque envoi
                await assert_public_destination(job.endpoint.url, self._resolver)
            response = await self._http.post(
                job.endpoint.url, content=body, headers=headers, follow_redirects=False
            )
            status_code = response.status_code
            error = None if 200 <= status_code < 300 else f"HTTP {status_code}"
        except UnsafeWebhookUrlError as exc:
            error = exc.detail
        except httpx.HTTPError as exc:
            error = type(exc).__name__
        await self._record(job, status_code=status_code, error=error)

    async def _record(self, job: _Job, *, status_code: int | None, error: str | None) -> None:
        values: dict[str, object] = {"locked_until": None, "last_status_code": status_code}
        if error is None:
            values |= {"status": "delivered", "delivered_at": func.now(), "last_error": None}
            _log.info("webhook_delivered", attempts=job.attempts)
        else:
            delay = next_delay_seconds(self._schedule, job.attempts, self._rng)
            values["last_error"] = error[:500]
            if delay is None:
                values["status"] = "dead_letter"
                _log.warning("webhook_dead_letter", attempts=job.attempts, error=error)
            else:
                values["next_attempt_at"] = func.now() + timedelta(seconds=delay)
                _log.info("webhook_retry_scheduled", attempts=job.attempts, in_seconds=delay)
        async with self._sessionmaker.begin() as session:
            await session.execute(
                update(WebhookDelivery)
                .where(WebhookDelivery.id == job.delivery_id)
                .values(**values)
            )
