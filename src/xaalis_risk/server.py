"""Serveur gRPC du scoring de risque (``grpc.aio``), avec le protocole de santé standard.

Le service est interne : aucun port publié par docker compose, seul le worker l'appelle.
Il ne journalise jamais un numéro en clair (XG-03) : uniquement l'intent, le score, la
décision et les raisons.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

import grpc
from grpc_health.v1 import health, health_pb2, health_pb2_grpc
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from xaalis.core.clock import DAKAR
from xaalis.observability.logging import get_logger
from xaalis.risk.v1 import risk_pb2, risk_pb2_grpc
from xaalis.worker.runner import install_signal_handlers
from xaalis_risk.scoring import Decision, VelocityTracker, assess, payer_fingerprint

SERVICE_NAME: Final = "xaalis.risk.v1.RiskScoring"
_log = get_logger("xaalis_risk")

DECISIONS: Final = {
    Decision.APPROVE: risk_pb2.DECISION_APPROVE,
    Decision.REVIEW: risk_pb2.DECISION_REVIEW,
    Decision.DECLINE: risk_pb2.DECISION_DECLINE,
}


class RiskSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="XAALIS_RISK_", extra="ignore")

    port: int = Field(default=50051, ge=1, le=65_535)
    # Numéros signalés pour fraude (E.164). Le défaut sert la démonstration : 77 000 00 00.
    blocklist: list[str] = Field(default_factory=lambda: ["+221770000000"])
    # Latence injectée (ms), pour montrer la deadline de 150 ms et le repli. 0 en temps normal.
    latency_ms: int = Field(default=0, ge=0, le=10_000)


class RiskScoringService(risk_pb2_grpc.RiskScoringServicer):
    def __init__(
        self,
        settings: RiskSettings,
        tracker: VelocityTracker | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._settings = settings
        self._tracker = tracker or VelocityTracker()
        self._clock = clock
        self._blocklist = {payer_fingerprint(phone) for phone in settings.blocklist}

    async def Score(  # noqa: N802 — nom imposé par le contrat gRPC
        self,
        request: risk_pb2.ScoreRequest,
        context: risk_pb2_grpc._ServicerContext,
    ) -> risk_pb2.ScoreResponse:
        if self._settings.latency_ms:
            await asyncio.sleep(self._settings.latency_ms / 1_000)
        if request.amount <= 0 or not request.payer_phone.startswith("+221"):
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "montant ou numéro invalide")
        fingerprint = payer_fingerprint(request.payer_phone)
        result = assess(
            amount=request.amount,
            recent_requests=self._tracker.hit(fingerprint),
            dakar_hour=self._clock().astimezone(DAKAR).hour,
            blocklisted=fingerprint in self._blocklist,
        )
        _log.info(
            "risk_scored",
            intent_id=request.payment_intent_id,
            score_permille=result.score_permille,
            decision=result.decision.value,
            reasons=list(result.reasons),
        )
        return risk_pb2.ScoreResponse(
            score_permille=result.score_permille,
            decision=DECISIONS[result.decision],
            reasons=list(result.reasons),
        )


@dataclass(frozen=True, slots=True)
class RiskServer:
    server: grpc.aio.Server
    health: health.aio.HealthServicer
    port: int

    async def start(self) -> None:
        await self.server.start()
        for service in (SERVICE_NAME, ""):
            await self.health.set(service, health_pb2.HealthCheckResponse.SERVING)

    async def stop(self, grace: int | None = None) -> None:
        await self.server.stop(grace)


def build_server(
    settings: RiskSettings,
    *,
    address: str | None = None,
    service: RiskScoringService | None = None,
) -> RiskServer:
    """Serveur prêt à démarrer ; ``address`` ``127.0.0.1:0`` choisit un port libre (tests)."""
    server = grpc.aio.server()
    risk_pb2_grpc.add_RiskScoringServicer_to_server(service or RiskScoringService(settings), server)
    checker = health.aio.HealthServicer()
    health_pb2_grpc.add_HealthServicer_to_server(checker, server)
    port = server.add_insecure_port(address or f"[::]:{settings.port}")
    return RiskServer(server, checker, port)


async def serve(settings: RiskSettings) -> None:
    """Sert jusqu'à SIGTERM/SIGINT, puis laisse 5 s aux appels en cours (docker stop)."""
    stop = asyncio.Event()
    install_signal_handlers(stop)
    risk = build_server(settings)
    await risk.start()
    _log.info("risk_service_started", port=risk.port)
    try:
        await stop.wait()
    finally:
        await risk.stop(grace=5)
        _log.info("risk_service_stopped")
