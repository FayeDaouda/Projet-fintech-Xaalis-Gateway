"""Simulateur des opérateurs mobile money (Go Pay, Teranga Money, Ndiaga Cash).

Squelette : le comportement de chaque opérateur (latence, échecs, callbacks, chaos,
relevés CSV) est ajouté au lot 3.
"""

from __future__ import annotations

from fastapi import FastAPI

from xaalis.api.middleware import RequestContextMiddleware
from xaalis.observability.logging import configure_logging

configure_logging(service="xaalis-operators")

app = FastAPI(title="Xaalis — simulateur d'opérateurs", version="0.1.0")
app.add_middleware(RequestContextMiddleware)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}
