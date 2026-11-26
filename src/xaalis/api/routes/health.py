"""Sondes d'exploitation : vivacité, disponibilité, métriques Prometheus.

- ``/healthz`` répond tant que le processus tourne (liveness) : il ne touche à aucune
  dépendance, pour qu'une panne de PostgreSQL ne fasse pas redémarrer l'API en boucle ;
- ``/readyz`` vérifie PostgreSQL et Redis (readiness) et renvoie 503 si l'un manque.
"""

from __future__ import annotations

import asyncio
import os
from typing import Literal

from fastapi import APIRouter, Response
from fastapi.responses import JSONResponse, PlainTextResponse
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    REGISTRY,
    CollectorRegistry,
    generate_latest,
    multiprocess,
)
from pydantic import BaseModel, ConfigDict
from sqlalchemy import text

from xaalis.api.resources import Resources, ResourcesDep
from xaalis.observability.business import refresh_business_metrics
from xaalis.observability.logging import get_logger

router = APIRouter(tags=["operations"])
_log = get_logger(__name__)

CheckStatus = Literal["ok", "error"]
_CHECK_TIMEOUT_SECONDS = 1.0


class Liveness(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"


class Readiness(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ready", "not_ready"]
    checks: dict[str, CheckStatus]


@router.get("/healthz", response_model=Liveness, summary="Sonde de vivacité")
async def healthz() -> Liveness:
    return Liveness()


@router.get(
    "/readyz",
    response_model=Readiness,
    summary="Sonde de disponibilité (PostgreSQL, Redis)",
    responses={503: {"model": Readiness, "description": "Une dépendance est indisponible."}},
)
async def readyz(resources: ResourcesDep) -> Response:
    database, redis = await asyncio.gather(_check_database(resources), _check_redis(resources))
    checks: dict[str, CheckStatus] = {"database": database, "redis": redis}
    ready = all(value == "ok" for value in checks.values())
    body = Readiness(status="ready" if ready else "not_ready", checks=checks)
    return JSONResponse(body.model_dump(), status_code=200 if ready else 503)


@router.get(
    "/metrics",
    response_class=PlainTextResponse,
    summary="Métriques au format d'exposition Prometheus",
)
async def metrics(resources: ResourcesDep) -> Response:
    await refresh_business_metrics(resources.sessionmaker, resources.operator_health)
    return Response(generate_latest(_registry()), media_type=CONTENT_TYPE_LATEST)


def _registry() -> CollectorRegistry:
    """Plusieurs processus uvicorn : on agrège les métriques de tous (mode multiprocessus),
    sinon /metrics ne montrerait que celles du processus qui a reçu la requête."""
    if not os.environ.get("PROMETHEUS_MULTIPROC_DIR"):
        return REGISTRY
    registry = CollectorRegistry()
    multiprocess.MultiProcessCollector(registry)  # type: ignore[no-untyped-call]  # non annoté en amont
    return registry


async def _check_database(resources: Resources) -> CheckStatus:
    try:
        async with asyncio.timeout(_CHECK_TIMEOUT_SECONDS), resources.engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 — toute panne rend l'instance non prête
        _log.warning("readiness_check_failed", dependency="database", error=type(exc).__name__)
        return "error"
    return "ok"


async def _check_redis(resources: Resources) -> CheckStatus:
    try:
        async with asyncio.timeout(_CHECK_TIMEOUT_SECONDS):
            await resources.redis.ping()
    except Exception as exc:  # noqa: BLE001
        _log.warning("readiness_check_failed", dependency="redis", error=type(exc).__name__)
        return "error"
    return "ok"
