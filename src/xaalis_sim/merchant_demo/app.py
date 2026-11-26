"""Marchand de démonstration : reçoit les webhooks Xaalis et sert la console web.

Récepteur de webhooks, comme un vrai client :

- vérifie ``X-Xaalis-Signature: t=…,v1=…`` (HMAC-SHA-256, tolérance 300 s, temps constant) ;
- déduplique par ``event_id`` : un événement relivré n'est traité qu'une fois ;
- l'ordre n'étant pas garanti, il ne fait jamais régresser un statut (``created_at`` fait foi) ;
- ``POST /_fail`` le rend volontairement indisponible (démo : relances puis dead letter) ;
- ``POST /_secrets`` ajoute le secret d'un endpoint créé à la volée (``scripts/demo.sh``),
  comme un marchand recopierait le secret affiché à la création de son endpoint.

Console web (``/``) : boutique, tableau de bord, webhooks, back-office et simulateur. Le
navigateur ne parle qu'à ``/api/*`` (``console.py``) ; la clé secrète reste sur ce serveur.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from xaalis.api.middleware import RequestContextMiddleware
from xaalis.observability.logging import configure_logging, get_logger
from xaalis_sim.merchant_demo.console import build_router, install_error_handlers
from xaalis_sim.merchant_demo.gateway import Gateway
from xaalis_sim.merchant_demo.inbox import WebhookInbox

STATIC = Path(__file__).parent / "static"


class RevalidatedStaticFiles(StaticFiles):
    """Fichiers statiques revalidés à chaque chargement (ETag) : jamais d'interface périmée."""

    def file_response(self, *args: Any, **kwargs: Any) -> Response:
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


_log = get_logger("xaalis_sim.merchant_demo")


class DemoSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="XAALIS_", extra="ignore")

    demo_webhook_secret: SecretStr = SecretStr("dev-only-demo-webhook-secret-00001")
    # Mêmes variables que la passerelle (docker-compose.yml) : une seule configuration.
    public_base_url: str = "http://localhost:8000"
    operators_base_url: str = "http://localhost:8100"
    merchant_demo_url: str = "http://localhost:8200/webhooks"
    seed_admin_email: str = "admin@xaalis.example"
    seed_admin_password: SecretStr = SecretStr("dev-only-admin-password")
    demo_shop_name: str = "Maison Sandaga"


class FailToggle(BaseModel):
    enabled: bool


class SecretIn(BaseModel):
    secret: str = Field(min_length=16, max_length=200)


def create_app(
    settings: DemoSettings | None = None,
    *,
    api_transport: httpx.AsyncBaseTransport | None = None,
    simulator_transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    config = settings or DemoSettings()
    inbox = WebhookInbox([config.demo_webhook_secret.get_secret_value()])
    timeout = httpx.Timeout(10.0)
    api = httpx.AsyncClient(
        base_url=config.public_base_url, transport=api_transport, timeout=timeout
    )
    simulator = httpx.AsyncClient(
        base_url=config.operators_base_url, transport=simulator_transport, timeout=timeout
    )
    gateway = Gateway(
        api=api,
        simulator=simulator,
        admin_email=config.seed_admin_email,
        admin_password=config.seed_admin_password.get_secret_value(),
        merchant_name=config.demo_shop_name,
        webhook_url=config.merchant_demo_url,
        on_endpoint_secret=inbox.add_secret,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        await api.aclose()
        await simulator.aclose()

    app = FastAPI(
        title="Xaalis — marchand de démonstration",
        version="1.0.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
    )
    app.add_middleware(RequestContextMiddleware)
    app.state.inbox = inbox
    app.state.gateway = gateway
    install_error_handlers(app)
    app.include_router(build_router(gateway, inbox))

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/webhooks")
    async def receive(request: Request) -> JSONResponse:
        body = await request.body()
        signature = request.headers.get("X-Xaalis-Signature", "")
        event = inbox.authenticate(signature, body, int(time.time()))
        if event is None:
            if inbox.failing:
                return JSONResponse({"error": "maintenance"}, status_code=503)
            _log.warning("webhook_rejected_bad_signature")
            return JSONResponse({"error": "invalid signature"}, status_code=400)
        outcome = inbox.record(event)
        if outcome == "unavailable":
            return JSONResponse({"error": "maintenance"}, status_code=503)
        _log.info("webhook_received", event_id=event["id"], type=event["type"], outcome=outcome)
        return JSONResponse({"received": True, "duplicate": outcome == "duplicate"})

    @app.get("/events")
    async def events() -> dict[str, Any]:
        processed = [entry for entry in inbox.journal.values() if entry.processed]
        return {
            "count": len(processed),
            "events": [{"id": e.event_id, "type": e.type} for e in processed[-50:]],
            "objects": inbox.objects,
        }

    @app.post("/_secrets", status_code=204)
    async def add_secret(body: SecretIn) -> None:
        inbox.add_secret(body.secret)

    @app.post("/_fail")
    async def toggle_failure(body: FailToggle) -> dict[str, bool]:
        inbox.failing = body.enabled
        _log.warning("merchant_demo_failure_mode", enabled=body.enabled)
        return {"failing": body.enabled}

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    app.mount("/static", RevalidatedStaticFiles(directory=STATIC), name="static")
    return app


configure_logging(service="xaalis-merchant-demo")
app = create_app()
