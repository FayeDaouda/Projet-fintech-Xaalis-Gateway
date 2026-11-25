"""Middleware ASGI de contexte de requête : request_id, journal d'accès, latence.

Écrit en ASGI pur plutôt qu'avec ``BaseHTTPMiddleware`` : ce dernier exécute la
route dans une tâche séparée (les contextvars posés ici ne s'y propagent pas de
façon fiable) et met en tampon les réponses en streaming, ce qui casserait le SSE.
"""

from __future__ import annotations

import re
import time
import uuid

import structlog
from starlette.datastructures import Headers, MutableHeaders
from starlette.routing import BaseRoute
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from xaalis.observability.logging import get_logger
from xaalis.observability.metrics import HTTP_REQUEST_DURATION

REQUEST_ID_HEADER = "X-Request-Id"
# On accepte l'identifiant fourni par l'appelant (traçabilité de bout en bout) seulement
# s'il est court et sans caractère exotique : pas d'injection dans les logs.
_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{8,128}$")
_UNMATCHED_ROUTE = "__unmatched__"

_log = get_logger("xaalis.access")


def _route_label(scope: Scope) -> str:
    """Gabarit de la route servie (posé par le routeur Starlette), jamais le chemin brut."""
    route = scope.get("route")
    path = getattr(route, "path", None) if isinstance(route, BaseRoute) else None
    return path if isinstance(path, str) else _UNMATCHED_ROUTE


def resolve_request_id(raw: str | None) -> str:
    if raw is not None and _REQUEST_ID_PATTERN.fullmatch(raw):
        return raw
    return f"req_{uuid.uuid4().hex}"


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = resolve_request_id(Headers(scope=scope).get(REQUEST_ID_HEADER))
        scope.setdefault("state", {})["request_id"] = request_id
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)

        status_code = 500
        started = time.perf_counter()

        async def send_with_request_id(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                MutableHeaders(scope=message)[REQUEST_ID_HEADER] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_with_request_id)
        finally:
            elapsed = time.perf_counter() - started
            route_label = _route_label(scope)
            HTTP_REQUEST_DURATION.labels(
                method=scope["method"], route=route_label, status=str(status_code)
            ).observe(elapsed)
            _log.info(
                "http_request",
                method=scope["method"],
                route=route_label,
                status=status_code,
                duration_ms=round(elapsed * 1000, 2),
            )
            structlog.contextvars.clear_contextvars()
