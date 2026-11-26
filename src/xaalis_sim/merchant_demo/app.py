"""Marchand de démonstration : reçoit les webhooks Xaalis et les vérifie comme un vrai client.

- vérifie ``X-Xaalis-Signature: t=…,v1=…`` (HMAC-SHA-256, tolérance 300 s, temps constant) ;
- déduplique par ``event_id`` : un événement relivré n'est traité qu'une fois ;
- l'ordre n'étant pas garanti, il ne fait jamais régresser un statut (``created_at`` fait foi) ;
- ``POST /_fail`` le rend volontairement indisponible (démo : relances puis dead letter) ;
- ``POST /_secrets`` ajoute le secret d'un endpoint créé à la volée (``scripts/demo.sh``),
  comme un marchand recopierait le secret affiché à la création de son endpoint.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from xaalis.api.middleware import RequestContextMiddleware
from xaalis.observability.logging import configure_logging, get_logger

TOLERANCE_SECONDS = 300
_log = get_logger("xaalis_sim.merchant_demo")


class DemoSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="XAALIS_", extra="ignore")

    demo_webhook_secret: SecretStr = SecretStr("dev-only-demo-webhook-secret-00001")


class FailToggle(BaseModel):
    enabled: bool


class SecretIn(BaseModel):
    secret: str = Field(min_length=16, max_length=200)


def verify_signature(secret: str, header: str, body: bytes, now: int) -> bool:
    parts = dict(item.split("=", 1) for item in header.split(",") if "=" in item)
    timestamp, received = parts.get("t", ""), parts.get("v1", "")
    if not timestamp.isdigit() or abs(now - int(timestamp)) > TOLERANCE_SECONDS:
        return False
    expected = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256)
    return hmac.compare_digest(expected.hexdigest().encode(), received.encode())


def create_app(settings: DemoSettings | None = None) -> FastAPI:
    config = settings or DemoSettings()
    app = FastAPI(title="Xaalis — marchand de démonstration", version="1.0.0")
    app.add_middleware(RequestContextMiddleware)
    state: dict[str, Any] = {"failing": False, "events": {}, "orders": {}}
    secrets: list[str] = [config.demo_webhook_secret.get_secret_value()]

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/webhooks")
    async def receive(request: Request) -> JSONResponse:
        if state["failing"]:
            return JSONResponse({"error": "maintenance"}, status_code=503)
        body = await request.body()
        signature = request.headers.get("X-Xaalis-Signature", "")
        now = int(time.time())
        if not any(verify_signature(secret, signature, body, now) for secret in secrets):
            _log.warning("webhook_rejected_bad_signature")
            return JSONResponse({"error": "invalid signature"}, status_code=400)

        event = json.loads(body)
        if event["id"] in state["events"]:
            return JSONResponse({"received": True, "duplicate": True})
        state["events"][event["id"]] = event
        obj = event["data"]["object"]
        # L'ordre n'est pas garanti : on ne garde que l'état le plus récent de chaque objet.
        current = state["orders"].get(obj["id"])
        if current is None or event["created_at"] >= current["event_created_at"]:
            state["orders"][obj["id"]] = {
                "status": obj.get("status"),
                "event_created_at": event["created_at"],
            }
        _log.info("webhook_received", event_id=event["id"], type=event["type"])
        return JSONResponse({"received": True, "duplicate": False})

    @app.get("/events")
    async def events() -> dict[str, Any]:
        received = list(state["events"].values())
        return {
            "count": len(received),
            "events": [{"id": e["id"], "type": e["type"]} for e in received[-50:]],
            "objects": state["orders"],
        }

    @app.post("/_secrets", status_code=204)
    async def add_secret(body: SecretIn) -> None:
        if body.secret not in secrets:
            secrets.append(body.secret)

    @app.post("/_fail")
    async def toggle_failure(body: FailToggle) -> dict[str, bool]:
        state["failing"] = body.enabled
        _log.warning("merchant_demo_failure_mode", enabled=body.enabled)
        return {"failing": body.enabled}

    return app


configure_logging(service="xaalis-merchant-demo")
app = create_app()
