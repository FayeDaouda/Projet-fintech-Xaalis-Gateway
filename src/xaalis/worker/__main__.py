"""Point d'entrée du worker : ``python -m xaalis.worker``."""

from __future__ import annotations

import asyncio
import functools
import os
from pathlib import Path

import httpx
from redis.asyncio import Redis

import xaalis.models  # noqa: F401 — enregistre TOUS les modèles ORM (clés étrangères)
from xaalis.config import get_settings
from xaalis.infra.db import create_engine, create_sessionmaker
from xaalis.observability.logging import configure_logging, get_logger
from xaalis.operators.client import OperatorClient, build_http_client
from xaalis.operators.dispatcher import Dispatcher
from xaalis.routing.health import OperatorHealth
from xaalis.webhooks.delivery import WebhookDeliverer
from xaalis.worker.jobs import (
    dispatch_job,
    expiry_job,
    idempotency_purge_job,
    webhook_delivery_job,
    webhook_fanout_job,
)
from xaalis.worker.runner import Job, heartbeat, install_signal_handlers, run_jobs

_DEFAULT_HEARTBEAT = "/tmp/xaalis-worker.heartbeat"  # noqa: S108 — fichier sans contenu sensible


async def main() -> None:
    settings = get_settings()
    configure_logging(
        service="xaalis-worker", level=settings.log_level, json_output=settings.log_json
    )
    log = get_logger("xaalis.worker")

    stop = asyncio.Event()
    install_signal_handlers(stop)
    heartbeat_path = Path(os.environ.get("XAALIS_HEALTHCHECK_FILE", _DEFAULT_HEARTBEAT))

    engine = create_engine(settings)
    sessionmaker = create_sessionmaker(engine)
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    http = build_http_client(
        settings.operators_base_url,
        connect_ms=settings.operator_connect_timeout_ms,
        read_ms=settings.operator_read_timeout_ms,
    )
    health = OperatorHealth(
        redis,
        failure_threshold=settings.breaker_failure_threshold,
        cooldown_seconds=settings.breaker_cooldown_seconds,
    )
    dispatcher = Dispatcher(
        sessionmaker, OperatorClient(http, callback_base_url=settings.public_base_url), health
    )
    webhook_http = httpx.AsyncClient(timeout=httpx.Timeout(settings.webhook_timeout_ms / 1000))
    deliverer = WebhookDeliverer(
        sessionmaker,
        webhook_http,
        encryption_key=settings.webhook_encryption_key.get_secret_value(),
        schedule=settings.webhook_retry_schedule_seconds,
    )
    jobs: list[Job] = [
        functools.partial(heartbeat, path=heartbeat_path),
        dispatch_job(dispatcher),
        expiry_job(sessionmaker),
        idempotency_purge_job(sessionmaker),
        webhook_fanout_job(sessionmaker),
        webhook_delivery_job(deliverer),
    ]
    log.info("worker_started", environment=settings.environment, jobs=len(jobs))
    try:
        await run_jobs(jobs, stop)
    finally:
        await http.aclose()
        await webhook_http.aclose()
        await redis.aclose()
        await engine.dispose()
        log.info("worker_stopped")


if __name__ == "__main__":
    asyncio.run(main())
