"""Point d'entrée du worker : ``python -m xaalis.worker``."""

from __future__ import annotations

import asyncio
import functools
import os
from datetime import date
from pathlib import Path

import httpx
from prometheus_client import start_http_server
from redis.asyncio import Redis

import xaalis.models  # noqa: F401 — enregistre TOUS les modèles ORM (clés étrangères)
from xaalis.config import get_settings
from xaalis.core.operators import Operator
from xaalis.infra.db import create_engine, create_sessionmaker
from xaalis.observability.logging import configure_logging, get_logger
from xaalis.observability.tracing import configure_tracing
from xaalis.operators.client import OperatorClient, build_http_client
from xaalis.operators.dispatcher import Dispatcher
from xaalis.risk.gate import RiskGate, open_channel
from xaalis.routing.health import OperatorHealth
from xaalis.webhooks.delivery import WebhookDeliverer
from xaalis.worker.jobs import (
    dispatch_job,
    expiry_job,
    idempotency_purge_job,
    nightly_job,
    reconciliation_job,
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
    configure_tracing("xaalis-worker")  # avant les clients httpx/gRPC : ils sont instrumentés
    metrics_port = int(os.environ.get("XAALIS_WORKER_METRICS_PORT", "0"))
    if metrics_port:  # métriques du worker (scoring, opérateurs) pour Prometheus
        start_http_server(metrics_port)

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
    risk_channel = open_channel(settings.risk_service_target)
    risk = RiskGate(
        risk_channel,
        deadline_ms=settings.risk_deadline_ms,
        fail_open_max_amount=settings.risk_fail_open_max_amount,
    )
    dispatcher = Dispatcher(
        sessionmaker,
        OperatorClient(http, callback_base_url=settings.public_base_url),
        health,
        risk=risk,
    )
    webhook_http = httpx.AsyncClient(timeout=httpx.Timeout(settings.webhook_timeout_ms / 1000))
    deliverer = WebhookDeliverer(
        sessionmaker,
        webhook_http,
        encryption_key=settings.webhook_encryption_key.get_secret_value(),
        schedule=settings.webhook_retry_schedule_seconds,
    )

    async def fetch_statement(operator: Operator, day: date) -> bytes:
        response = await http.get(f"/{operator.value}/statements/{day.isoformat()}.csv")
        response.raise_for_status()
        return response.content

    jobs: list[Job] = [
        functools.partial(heartbeat, path=heartbeat_path),
        dispatch_job(dispatcher),
        expiry_job(sessionmaker),
        idempotency_purge_job(sessionmaker),
        webhook_fanout_job(sessionmaker),
        webhook_delivery_job(deliverer),
        reconciliation_job(sessionmaker, fetch_statement),
        nightly_job(sessionmaker),
    ]
    log.info("worker_started", environment=settings.environment, jobs=len(jobs))
    try:
        await run_jobs(jobs, stop)
    finally:
        await http.aclose()
        await risk_channel.close()
        await webhook_http.aclose()
        await redis.aclose()
        await engine.dispose()
        log.info("worker_stopped")


if __name__ == "__main__":
    asyncio.run(main())
