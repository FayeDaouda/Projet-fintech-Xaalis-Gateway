"""Point d'entrée du worker : ``python -m xaalis.worker``."""

from __future__ import annotations

import asyncio
import functools
import os
from pathlib import Path

import xaalis.models  # noqa: F401 — enregistre TOUS les modèles ORM (clés étrangères)
from xaalis.config import get_settings
from xaalis.infra.db import create_engine, create_sessionmaker
from xaalis.observability.logging import configure_logging, get_logger
from xaalis.worker.jobs import expiry_job, idempotency_purge_job
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
    jobs: list[Job] = [
        functools.partial(heartbeat, path=heartbeat_path),
        expiry_job(sessionmaker),
        idempotency_purge_job(sessionmaker),
    ]
    log.info("worker_started", environment=settings.environment, jobs=len(jobs))
    try:
        await run_jobs(jobs, stop)
    finally:
        await engine.dispose()
        log.info("worker_stopped")


if __name__ == "__main__":
    asyncio.run(main())
