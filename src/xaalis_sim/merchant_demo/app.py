"""Marchand de démonstration : reçoit les webhooks Xaalis et vérifie leur signature.

Squelette : la vérification HMAC et le journal des événements reçus arrivent au lot 5.
"""

from __future__ import annotations

from fastapi import FastAPI

from xaalis.api.middleware import RequestContextMiddleware
from xaalis.observability.logging import configure_logging

configure_logging(service="xaalis-merchant-demo")

app = FastAPI(title="Xaalis — marchand de démonstration", version="0.1.0")
app.add_middleware(RequestContextMiddleware)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}
