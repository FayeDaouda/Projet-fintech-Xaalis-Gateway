"""Bonus gRPC (ADR 0013) : vrai serveur ``grpc.aio`` démarré dans le test, vraie deadline.

Ce que l'énoncé exige : appel avec une deadline de 150 ms, politique de repli justifiée.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import grpc
import pytest

from xaalis.infra.healthcheck import check_grpc
from xaalis.risk.gate import RiskDecision, RiskGate, RiskQuery, RiskSource
from xaalis.risk.v1 import risk_pb2, risk_pb2_grpc
from xaalis_risk.server import RiskScoringService, RiskServer, RiskSettings, build_server

NOON_IN_DAKAR = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)  # Dakar = UTC toute l'année


async def _start(settings: RiskSettings) -> RiskServer:
    service = RiskScoringService(settings, clock=lambda: NOON_IN_DAKAR)
    server = build_server(settings, address="127.0.0.1:0", service=service)
    await server.start()
    return server


@pytest.fixture
async def risk_server() -> AsyncIterator[RiskServer]:
    server = await _start(RiskSettings(blocklist=["+221770000000"]))
    yield server
    await server.stop(None)


def _query(amount: int = 12_345, phone: str = "+221781234567") -> RiskQuery:
    return RiskQuery(
        intent_id="pi_01JD8Q3W7Z4K2N6R5T9V0XBCYA",
        merchant_id="m_01JD8Q3W7Z4K2N6R5T9V0XBCYA",
        livemode=False,
        amount=amount,
        payer_phone=phone,
    )


async def test_service_approves_an_ordinary_payment(risk_server: RiskServer) -> None:
    async with grpc.aio.insecure_channel(f"127.0.0.1:{risk_server.port}") as channel:
        verdict = await RiskGate(channel).score(_query())
    assert verdict.decision is RiskDecision.APPROVE
    assert verdict.source is RiskSource.SERVICE
    assert verdict.score_permille == 0


async def test_service_declines_a_blocklisted_payer(risk_server: RiskServer) -> None:
    async with grpc.aio.insecure_channel(f"127.0.0.1:{risk_server.port}") as channel:
        verdict = await RiskGate(channel).score(_query(phone="+221770000000"))
    assert verdict.decision is RiskDecision.DECLINE
    assert verdict.reasons == ("payer_blocklisted",)


async def test_invalid_request_is_rejected_with_invalid_argument(risk_server: RiskServer) -> None:
    async with grpc.aio.insecure_channel(f"127.0.0.1:{risk_server.port}") as channel:
        stub = risk_pb2_grpc.RiskScoringStub(channel)
        with pytest.raises(grpc.aio.AioRpcError) as error:
            await stub.Score(risk_pb2.ScoreRequest(amount=0, payer_phone="+221781234567"))
    assert error.value.code() is grpc.StatusCode.INVALID_ARGUMENT


async def test_bonus_deadline_150ms_slow_service_falls_back_quickly() -> None:
    server = await _start(RiskSettings(latency_ms=400))  # le service répond trop tard
    try:
        async with grpc.aio.insecure_channel(f"127.0.0.1:{server.port}") as channel:
            gate = RiskGate(channel, deadline_ms=150, fail_open_max_amount=100_000)
            loop = asyncio.get_running_loop()
            started = loop.time()
            small = await gate.score(_query(amount=50_000))
            elapsed = loop.time() - started
            large = await gate.score(_query(amount=150_000))
    finally:
        await server.stop(None)

    assert elapsed < 0.35, f"la deadline de 150 ms n'a pas été tenue ({elapsed:.3f} s)"
    assert (small.decision, small.source) == (RiskDecision.APPROVE, RiskSource.FALLBACK)
    assert small.reasons == ("risk_unavailable_deadline_exceeded",)
    assert large.decision is None  # au-dessus du plafond : le paiement attend
    assert "held_above_ceiling" in large.reasons


async def test_unreachable_service_triggers_the_fallback_policy() -> None:
    async with grpc.aio.insecure_channel("127.0.0.1:1") as channel:  # personne n'écoute
        gate = RiskGate(channel, fail_open_max_amount=100_000)
        small = await gate.score(_query(amount=100_000))  # plafond inclus
        large = await gate.score(_query(amount=100_001))
    assert small.decision is RiskDecision.APPROVE
    assert small.reasons == ("risk_unavailable_unavailable",)
    assert large.decision is None


async def test_health_probe_speaks_the_standard_grpc_health_protocol(
    risk_server: RiskServer,
) -> None:
    healthy = await asyncio.to_thread(check_grpc, f"127.0.0.1:{risk_server.port}")
    assert healthy
    assert not await asyncio.to_thread(check_grpc, "127.0.0.1:1", 0.5)
    assert not check_grpc(f"10.0.0.1:{risk_server.port}")  # jamais une cible distante
