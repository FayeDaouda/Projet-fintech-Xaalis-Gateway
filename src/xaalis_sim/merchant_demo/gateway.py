"""Client de la passerelle, côté serveur du marchand de démonstration.

C'est ici, et seulement ici, que vit la clé secrète : le navigateur ne parle qu'au serveur
du marchand, qui signe et relaie (modèle « clé secrète côté serveur » de tout PSP).

Le module ne dépend volontairement d'aucun module interne de la passerelle : il l'appelle
comme le ferait le SDK d'un vrai client, par HTTP et selon le contrat OpenAPI.

- Intégration paresseuse : au premier besoin, le marchand est créé par l'API
  d'administration, son endpoint de webhooks enregistré et le secret de signature recopié
  dans le récepteur (``on_endpoint_secret``), comme le ferait un intégrateur.
- Écritures sensibles signées : ``X-Xaalis-Signature = HMAC-SHA-256(sk, "ts.corps")``,
  calculée sur les octets exacts envoyés.
- Idempotence de bout en bout : la clé fournie par le navigateur (un double clic renvoie
  la même) est relayée telle quelle ; à défaut, une clé neuve par appel.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

WEBHOOK_EVENTS = (
    "payment.succeeded",
    "payment.failed",
    "payment.partially_refunded",
    "payment.refunded",
    "refund.succeeded",
    "payout.paid",
)


class GatewayUnavailableError(Exception):
    """La passerelle (ou le simulateur) ne répond pas : le serveur marchand renvoie 502."""


class OnboardingError(Exception):
    """L'intégration automatique a échoué : réponse inattendue de l'API d'administration."""

    def __init__(self, step: str, response: httpx.Response) -> None:
        super().__init__(f"{step} : HTTP {response.status_code}")
        self.step = step
        self.response = response


@dataclass(frozen=True, slots=True)
class MerchantSession:
    merchant_id: str
    name: str
    secret_key: str
    publishable_key: str
    endpoint_id: str
    endpoint_url: str
    created_at: datetime

    def public_view(self) -> dict[str, Any]:
        """Ce que le navigateur peut voir : jamais la clé secrète ni le secret d'endpoint."""
        return {
            "merchant_id": self.merchant_id,
            "name": self.name,
            "livemode": False,
            "publishable_key": self.publishable_key,
            "secret_key_hint": f"{self.secret_key[:8]}…{self.secret_key[-4:]}",
            "webhook_endpoint": {"id": self.endpoint_id, "url": self.endpoint_url},
            "created_at": self.created_at.isoformat(),
        }


def sign_request(secret_key: str, timestamp: int, body: bytes) -> str:
    """Signature d'une requête marchand : HMAC-SHA-256(sk, "timestamp." + corps brut)."""
    message = str(timestamp).encode() + b"." + body
    return hmac.new(secret_key.encode(), message, hashlib.sha256).hexdigest()


def encode_json(payload: Mapping[str, Any]) -> bytes:
    """Sérialisation unique : ce sont exactement ces octets qui sont signés puis envoyés."""
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()


class Gateway:
    def __init__(
        self,
        *,
        api: httpx.AsyncClient,
        simulator: httpx.AsyncClient,
        admin_email: str,
        admin_password: str,
        merchant_name: str,
        webhook_url: str,
        on_endpoint_secret: Callable[[str], None],
    ) -> None:
        self.api = api
        self.simulator = simulator
        self._admin_email = admin_email
        self._admin_password = admin_password
        self._merchant_name = merchant_name
        self._webhook_url = webhook_url
        self._on_endpoint_secret = on_endpoint_secret
        self._lock = asyncio.Lock()
        self._session: MerchantSession | None = None
        self._admin_token: str | None = None

    # ------------------------------------------------------------------ session marchand

    async def merchant(self) -> MerchantSession:
        """Le marchand courant ; créé une seule fois, même sous requêtes concurrentes."""
        async with self._lock:
            if self._session is None:
                self._session = await self._onboard()
            return self._session

    async def reset(self) -> MerchantSession:
        """Nouveau marchand (chiffres remis à zéro pour une nouvelle démonstration)."""
        async with self._lock:
            self._session = await self._onboard()
            return self._session

    async def _onboard(self) -> MerchantSession:
        created = await self.admin("POST", "/v1/admin/merchants", {"name": self._merchant_name})
        if created.status_code != 201:
            raise OnboardingError("création du marchand", created)
        merchant = created.json()
        endpoint = await self._send(
            "POST",
            "/v1/webhook-endpoints",
            {"Authorization": f"Bearer {merchant['secret_key']}"},
            {"url": self._webhook_url, "events": list(WEBHOOK_EVENTS)},
        )
        if endpoint.status_code != 201:
            raise OnboardingError("enregistrement de l'endpoint de webhooks", endpoint)
        registered = endpoint.json()
        self._on_endpoint_secret(registered["secret"])
        return MerchantSession(
            merchant_id=merchant["id"],
            name=merchant["name"],
            secret_key=merchant["secret_key"],
            publishable_key=merchant["publishable_key"],
            endpoint_id=registered["id"],
            endpoint_url=registered["url"],
            created_at=datetime.now(UTC),
        )

    # ------------------------------------------------------------------ appels

    async def call(
        self,
        method: str,
        path: str,
        payload: Mapping[str, Any] | None = None,
        *,
        params: Mapping[str, str] | None = None,
        idempotency_key: str | None = None,
        idempotent: bool = False,
        signed: bool = False,
    ) -> httpx.Response:
        """Appel avec la clé secrète du marchand (signé et/ou idempotent si demandé)."""
        session = await self.merchant()
        headers = {"Authorization": f"Bearer {session.secret_key}"}
        if idempotent:
            headers["Idempotency-Key"] = idempotency_key or str(uuid.uuid4())
        body = encode_json(payload) if payload is not None else b""
        if signed:
            timestamp = int(time.time())
            headers["X-Xaalis-Timestamp"] = str(timestamp)
            headers["X-Xaalis-Signature"] = sign_request(session.secret_key, timestamp, body)
        return await self._send(method, path, headers, payload, body=body, params=params)

    async def graphql(self, query: str, variables: Mapping[str, Any]) -> httpx.Response:
        return await self.call("POST", "/graphql", {"query": query, "variables": dict(variables)})

    async def admin(
        self, method: str, path: str, payload: Mapping[str, Any] | None = None
    ) -> httpx.Response:
        """Appel d'administration ; le jeton est renouvelé une fois s'il a expiré."""
        if self._admin_token is None:
            self._admin_token = await self._login()
        response = await self._send(
            method, path, {"Authorization": f"Bearer {self._admin_token}"}, payload
        )
        if response.status_code == 401:
            self._admin_token = await self._login()
            response = await self._send(
                method, path, {"Authorization": f"Bearer {self._admin_token}"}, payload
            )
        return response

    async def _login(self) -> str:
        response = await self._send(
            "POST",
            "/v1/admin/auth/token",
            {},
            {"email": self._admin_email, "password": self._admin_password},
        )
        if response.status_code != 200:
            raise OnboardingError("connexion administrateur", response)
        token: str = response.json()["access_token"]
        return token

    async def _send(
        self,
        method: str,
        path: str,
        headers: Mapping[str, str],
        payload: Mapping[str, Any] | None,
        *,
        body: bytes | None = None,
        params: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        if body is None and payload is not None:
            body = encode_json(payload)
        all_headers = dict(headers)
        if payload is not None:
            all_headers["Content-Type"] = "application/json"
        try:
            return await self.api.request(
                method, path, headers=all_headers, content=body or None, params=params
            )
        except httpx.TransportError as exc:
            raise GatewayUnavailableError(str(exc) or type(exc).__name__) from exc

    async def sim(
        self, method: str, path: str, payload: Mapping[str, Any] | None = None
    ) -> httpx.Response:
        try:
            return await self.simulator.request(method, path, json=payload)
        except httpx.TransportError as exc:
            raise GatewayUnavailableError(str(exc) or type(exc).__name__) from exc
