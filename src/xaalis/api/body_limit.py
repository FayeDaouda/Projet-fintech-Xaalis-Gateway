"""Taille maximale des corps de requête (OWASP API4 : consommation de ressources).

Deux contrôles, en ASGI pur :

1. ``Content-Length`` déclaré trop grand → 413 immédiat, sans rien lire ;
2. corps envoyé en flux (``chunked``) ou ``Content-Length`` mensonger → on lit au plus
   ``limite`` octets ; au-delà, 413. Le corps lu est rejoué tel quel à l'application.

La limite est de 1 Mio par défaut ; certaines routes (dépôt d'un relevé CSV) en ont une
plus haute, déclarée par préfixe de chemin.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Final

from starlette.datastructures import Headers
from starlette.types import ASGIApp, Message, Receive, Scope, Send

DEFAULT_LIMIT: Final = 1024 * 1024
PROBLEM_TYPE: Final = "https://docs.xaalis.example/problems/payload-too-large"


class BodySizeLimitMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        *,
        default_limit: int = DEFAULT_LIMIT,
        overrides: Mapping[str, int] | None = None,
    ) -> None:
        self.app = app
        self.default_limit = default_limit
        self.overrides = dict(overrides or {})

    def _limit_for(self, path: str) -> int:
        for prefix, limit in self.overrides.items():
            if path.startswith(prefix):
                return limit
        return self.default_limit

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        limit = self._limit_for(scope["path"])
        declared = Headers(scope=scope).get("content-length")
        if declared is not None and declared.isdigit() and int(declared) > limit:
            await self._reject(scope, send, limit)
            return

        # Corps lu ici, borné, puis rejoué à l'application : une erreur levée pendant la
        # lecture serait sinon déguisée en « corps illisible » (400) par le framework.
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] != "http.request":
                break  # client déconnecté : on laisse l'application le constater
            body.extend(message.get("body", b""))
            if len(body) > limit:
                await self._reject(scope, send, limit)
                return
            if not message.get("more_body", False):
                break

        replayed = False

        async def replay() -> Message:
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()  # ensuite : messages de déconnexion (flux SSE)

        await self.app(scope, replay, send)

    @staticmethod
    async def _reject(scope: Scope, send: Send, limit: int) -> None:
        body = json.dumps(
            {
                "type": PROBLEM_TYPE,
                "title": "Content Too Large",
                "status": 413,
                "detail": f"Le corps de la requête dépasse {limit} octets.",
                "instance": scope["path"],
                "code": "payload_too_large",
            }
        ).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/problem+json"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
