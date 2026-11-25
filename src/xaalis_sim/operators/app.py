"""Simulateur des opérateurs mobile money (Go Pay, Teranga Money, Ndiaga Cash).

API exposée à la passerelle :

- ``POST /{operator}/charges``            demande de débit (idempotente sur ``reference``) ;
- ``GET  /{operator}/charges/{reference}`` statut d'une charge (levée d'ambiguïté) ;
- ``POST /_chaos`` / ``GET`` / ``DELETE``  forcer une panne, une latence, un taux d'échec.

Après la latence propre à l'opérateur, le simulateur rappelle ``callback_url`` dans son
format et avec sa signature. Les charges sont conservées en mémoire (relevés du lot 6).
"""

from __future__ import annotations

import asyncio
import random
import re
import secrets
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from xaalis.api.middleware import RequestContextMiddleware
from xaalis.observability.logging import configure_logging, get_logger
from xaalis_sim.operators.formats import BUILDERS
from xaalis_sim.operators.profiles import PROFILES, Profile

OperatorName = Literal["gopay", "teranga", "ndiaga"]
_PHONE = re.compile(r"^\+2217(\d)\d{7}$")
_log = get_logger("xaalis_sim.operators")


class SimulatorSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="XAALIS_", extra="ignore")

    # Mêmes variables que la passerelle : les deux côtés partagent le secret de callback.
    operator_secret_gopay: SecretStr = SecretStr("dev-only-gopay-callback-secret-0001")
    operator_secret_teranga: SecretStr = SecretStr("dev-only-teranga-callback-secret-0001")
    operator_secret_ndiaga: SecretStr = SecretStr("dev-only-ndiaga-callback-secret-0001")
    sim_seed: int | None = None
    # Fenêtres de panne périodiques de Ndiaga Cash : toutes les N s, pendant M s (0 = jamais).
    sim_ndiaga_outage_every_seconds: int = Field(default=0, ge=0)
    sim_ndiaga_outage_duration_seconds: int = Field(default=0, ge=0)
    sim_callback_attempts: int = Field(default=3, ge=1)


@dataclass
class Chaos:
    outage_until: float = 0.0
    failure_rate: float | None = None
    latency_ms: tuple[int, int] | None = None
    response_delay_ms: int = 0
    duplicate_rate: float | None = None


@dataclass
class Charge:
    operator: str
    reference: str
    operator_reference: str
    amount: int
    payer_phone: str
    callback_url: str
    status: str = "pending"
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    completed_at: datetime | None = None
    callbacks_sent: int = 0
    request_id: str | None = None


class ChargeRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    reference: str = Field(min_length=1, max_length=64)
    amount: int = Field(gt=0)
    currency: Literal["XOF"]
    payer_phone: str
    callback_url: str


class ChaosRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operator: OperatorName
    outage_seconds: int | None = Field(default=None, ge=0, le=3_600)
    failure_rate: float | None = Field(default=None, ge=0, le=1)
    latency_ms: tuple[int, int] | None = None
    response_delay_ms: int | None = Field(default=None, ge=0, le=60_000)
    duplicate_rate: float | None = Field(default=None, ge=0, le=1)


class Simulator:
    def __init__(self, settings: SimulatorSettings, callback_client: httpx.AsyncClient) -> None:
        self.settings = settings
        self.http = callback_client
        self.rng = random.Random(settings.sim_seed)  # noqa: S311 — simulation, pas de crypto
        self.chaos: dict[str, Chaos] = {name: Chaos() for name in PROFILES}
        self.charges: dict[tuple[str, str], Charge] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self._started = time.monotonic()

    def secret(self, operator: str) -> str:
        value: SecretStr = getattr(self.settings, f"operator_secret_{operator}")
        return value.get_secret_value()

    def in_outage(self, operator: str) -> bool:
        if time.monotonic() < self.chaos[operator].outage_until:
            return True
        every = self.settings.sim_ndiaga_outage_every_seconds
        duration = self.settings.sim_ndiaga_outage_duration_seconds
        if operator == "ndiaga" and every > 0 and duration > 0:
            return (time.monotonic() - self._started) % every < duration
        return False

    def _effective(self, profile: Profile) -> tuple[tuple[int, int], float, float]:
        chaos = self.chaos[profile.name]
        latency = chaos.latency_ms or profile.latency_ms
        failure = profile.failure_rate if chaos.failure_rate is None else chaos.failure_rate
        duplicate = profile.duplicate_rate if chaos.duplicate_rate is None else chaos.duplicate_rate
        return latency, failure, duplicate

    def schedule(self, charge: Charge, request_id: str | None) -> None:
        task = asyncio.create_task(self._complete(charge, request_id))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _complete(self, charge: Charge, request_id: str | None) -> None:
        profile = PROFILES[charge.operator]
        (low, high), failure_rate, duplicate_rate = self._effective(profile)
        await asyncio.sleep(self.rng.randint(low, max(low, high)) / 1000)
        ok = self.rng.random() >= failure_rate
        charge.status = "succeeded" if ok else "failed"
        charge.completed_at = datetime.now(UTC)
        copies = 2 if self.rng.random() < duplicate_rate else 1
        callback = BUILDERS[charge.operator](
            self.secret(charge.operator),
            reference=charge.reference,
            operator_reference=charge.operator_reference,
            amount=charge.amount,
            ok=ok,
        )
        headers = {**callback.headers, **({"X-Request-Id": request_id} if request_id else {})}
        for _ in range(copies):
            await self._deliver(charge, callback.body, headers)

    async def _deliver(self, charge: Charge, body: bytes, headers: dict[str, str]) -> None:
        for attempt in range(1, self.settings.sim_callback_attempts + 1):
            try:
                response = await self.http.post(charge.callback_url, content=body, headers=headers)
                if response.status_code < 500:
                    charge.callbacks_sent += 1
                    _log.info(
                        "callback_delivered",
                        operator=charge.operator,
                        reference=charge.reference,
                        status=response.status_code,
                    )
                    return
            except httpx.HTTPError as exc:
                _log.warning("callback_failed", operator=charge.operator, error=type(exc).__name__)
            await asyncio.sleep(min(attempt, 3))

    async def drain(self) -> None:
        """Attend la fin des callbacks en cours (tests)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)


def create_app(
    settings: SimulatorSettings | None = None, callback_client: httpx.AsyncClient | None = None
) -> FastAPI:
    sim = Simulator(
        settings or SimulatorSettings(),
        callback_client or httpx.AsyncClient(timeout=httpx.Timeout(5.0)),
    )
    app = FastAPI(title="Xaalis — simulateur d'opérateurs", version="1.0.0")
    app.add_middleware(RequestContextMiddleware)
    app.state.simulator = sim

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/{operator}/charges", status_code=202)
    async def create_charge(
        operator: OperatorName,
        body: ChargeRequest,
        request: Request,
        x_request_id: Annotated[str | None, Header()] = None,
    ) -> JSONResponse:
        chaos = sim.chaos[operator]
        if chaos.response_delay_ms:
            await asyncio.sleep(chaos.response_delay_ms / 1000)
        if sim.in_outage(operator):
            return JSONResponse({"error": "service_unavailable"}, status_code=503)
        match = _PHONE.fullmatch(body.payer_phone)
        prefix = f"7{match.group(1)}" if match else ""
        if prefix not in PROFILES[operator].prefixes:
            return JSONResponse({"error": "unsupported_number"}, status_code=422)

        key = (operator, body.reference)
        existing = sim.charges.get(key)
        if existing is not None:  # idempotence : même référence → même charge, aucun re-débit
            return JSONResponse(
                {"operator_reference": existing.operator_reference, "status": existing.status},
                status_code=200,
            )
        charge = Charge(
            operator=operator,
            reference=body.reference,
            operator_reference=f"{PROFILES[operator].reference_prefix}-{secrets.token_hex(8).upper()}",
            amount=body.amount,
            payer_phone=body.payer_phone,
            callback_url=body.callback_url,
        )
        charge.request_id = x_request_id or getattr(request.state, "request_id", None)
        sim.charges[key] = charge
        sim.schedule(charge, charge.request_id)
        return JSONResponse(
            {"operator_reference": charge.operator_reference, "status": "pending"}, status_code=202
        )

    @app.get("/{operator}/charges/{reference}")
    async def get_charge(operator: OperatorName, reference: str) -> dict[str, Any]:
        charge = sim.charges.get((operator, reference))
        if charge is None:
            raise HTTPException(404, "unknown charge")
        return {"operator_reference": charge.operator_reference, "status": charge.status}

    def chaos_view() -> dict[str, Any]:
        now = time.monotonic()
        return {
            name: {
                "outage": sim.in_outage(name),
                "outage_remaining_seconds": max(0, round(state.outage_until - now)),
                "failure_rate": sim._effective(PROFILES[name])[1],
                "latency_ms": list(sim._effective(PROFILES[name])[0]),
                "duplicate_rate": sim._effective(PROFILES[name])[2],
                "response_delay_ms": state.response_delay_ms,
            }
            for name, state in sim.chaos.items()
        }

    @app.post("/_chaos")
    async def set_chaos(body: ChaosRequest) -> dict[str, Any]:
        state = sim.chaos[body.operator]
        if body.outage_seconds is not None:
            state.outage_until = time.monotonic() + body.outage_seconds
        if body.failure_rate is not None:
            state.failure_rate = body.failure_rate
        if body.latency_ms is not None:
            state.latency_ms = body.latency_ms
        if body.response_delay_ms is not None:
            state.response_delay_ms = body.response_delay_ms
        if body.duplicate_rate is not None:
            state.duplicate_rate = body.duplicate_rate
        _log.warning("chaos_updated", **body.model_dump(exclude_none=True))
        return chaos_view()

    @app.get("/_chaos")
    async def get_chaos() -> dict[str, Any]:
        return chaos_view()

    @app.delete("/_chaos")
    async def reset_chaos() -> dict[str, Any]:
        sim.chaos = {name: Chaos() for name in PROFILES}
        return chaos_view()

    return app


configure_logging(service="xaalis-operators")
app = create_app()
