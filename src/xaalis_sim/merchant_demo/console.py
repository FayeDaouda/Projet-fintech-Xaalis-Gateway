"""API de la console web (``/api/*``) : le « backend » du marchand de démonstration.

Le navigateur n'appelle que ces routes ; elles relaient vers la passerelle avec la clé
secrète, signent ce qui doit l'être et renvoient les erreurs de la passerelle telles
quelles (``application/problem+json``, même statut), pour que l'interface affiche le vrai
message du contrat. Outil de démonstration local : il détient aussi les identifiants
d'administration de développement (back-office : rapprochement, règlement).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import re
import secrets
from collections.abc import AsyncIterator
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo

import httpx
from fastapi import APIRouter, FastAPI, Header, Path, Query, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from xaalis_sim.merchant_demo import queries
from xaalis_sim.merchant_demo.gateway import Gateway, GatewayUnavailableError, OnboardingError
from xaalis_sim.merchant_demo.inbox import WebhookInbox

DAKAR = ZoneInfo("Africa/Dakar")
OPERATORS = ("gopay", "teranga", "ndiaga")
IntentStatusFilter = Literal[
    "created",
    "processing",
    "succeeded",
    "failed",
    "canceled",
    "expired",
    "partially_refunded",
    "refunded",
]
OperatorName = Literal["gopay", "teranga", "ndiaga"]
# Identifiants relayés dans une URL amont : forme stricte, aucun « / » ni « .. » possible.
IntentId = Annotated[str, Path(pattern=r"^pi_[0-9A-Za-z]{10,40}$")]
EventId = Annotated[str, Path(pattern=r"^evt_[0-9A-Za-z]{10,40}$")]
IdempotencyKey = Annotated[str | None, Header(alias="Idempotency-Key", max_length=255)]


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CheckoutIn(Input):
    amount: int = Field(ge=1, le=10_000_000)
    payer_phone: str = Field(min_length=9, max_length=20)
    description: str | None = Field(default=None, max_length=255)
    order_id: str | None = Field(default=None, max_length=64)


class DemoPaymentIn(Input):
    kind: Literal["normal", "suspect"] = "normal"


DEMO_BLOCKED_PHONE = "+221770000000"  # liste noire de démonstration du service risk
_DEMO_PREFIXES = ("78", "77", "76", "70", "75")
_DEMO_AMOUNTS = (2_500, 3_500, 5_000, 7_500, 12_500, 15_000, 18_000, 24_000, 32_500, 45_000)


def random_demo_phone() -> str:
    prefix = _DEMO_PREFIXES[secrets.randbelow(len(_DEMO_PREFIXES))]
    return f"+221{prefix}{secrets.randbelow(10**7):07d}"


def random_demo_amount() -> int:
    return _DEMO_AMOUNTS[secrets.randbelow(len(_DEMO_AMOUNTS))]


class RefundIn(Input):
    amount: int = Field(ge=1, le=10_000_000)
    reason: str | None = Field(default=None, max_length=255)


class FailureIn(Input):
    enabled: bool


class OutageIn(Input):
    operator: OperatorName
    seconds: int = Field(ge=0, le=3_600)


class DayIn(Input):
    date: dt.date


class SettlementIn(Input):
    date: dt.date
    as_of: dt.date | None = None


def dakar_today() -> dt.date:
    return dt.datetime.now(DAKAR).date()


# --------------------------------------------------------------------------- relais


def relay(response: httpx.Response) -> Response:
    """Renvoie la réponse amont telle quelle (corps, statut, type de contenu)."""
    return Response(
        content=response.content,
        status_code=response.status_code,
        media_type=response.headers.get("content-type", "application/json"),
    )


def unavailable(service: str, error: Exception) -> JSONResponse:
    return JSONResponse(
        {
            "type": "about:blank",
            "title": f"{service} injoignable",
            "status": 502,
            "detail": str(error),
            "code": "upstream_unavailable",
        },
        status_code=502,
        media_type="application/problem+json",
    )


_CAMEL = re.compile(r"(?<!^)(?=[A-Z])")


def normalize(value: Any, key: str = "") -> Any:
    """GraphQL → forme REST : clés en snake_case, énumérations en minuscules."""
    if isinstance(value, dict):
        return {_CAMEL.sub("_", k).lower(): normalize(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [normalize(item, key) for item in value]
    if key in {"status", "operator"} and isinstance(value, str):
        return value.lower()
    return value


class UpstreamProblemError(Exception):
    """Réponse d'erreur amont à renvoyer telle quelle au navigateur."""

    def __init__(self, response: Response) -> None:
        super().__init__(response.status_code)
        self.response = response


async def graphql_data(gateway: Gateway, query: str, variables: dict[str, Any]) -> Any:
    response = await gateway.graphql(query, variables)
    if response.status_code != 200:
        raise UpstreamProblemError(relay(response))
    body = response.json()
    if body.get("errors"):
        message = "; ".join(error.get("message", "?") for error in body["errors"])
        raise UpstreamProblemError(
            JSONResponse(
                {"title": "Requête de reporting refusée", "status": 502, "detail": message},
                status_code=502,
                media_type="application/problem+json",
            )
        )
    return normalize(body["data"])


# --------------------------------------------------------------------------- routes


def build_router(
    gateway: Gateway, inbox: WebhookInbox, *, links: dict[str, str] | None = None
) -> APIRouter:
    router = APIRouter(prefix="/api", tags=["console"])

    # -- session ---------------------------------------------------------------------
    @router.get("/health")
    async def health() -> Response:
        return relay(await gateway.api.get("/readyz"))

    @router.get("/session")
    async def session() -> dict[str, Any]:
        merchant = await gateway.merchant()
        return {
            **merchant.public_view(),
            "today": dakar_today().isoformat(),
            "links": links or {},
        }

    @router.post("/session/reset")
    async def reset_session() -> dict[str, Any]:
        merchant = await gateway.reset()
        return {
            **merchant.public_view(),
            "today": dakar_today().isoformat(),
            "links": links or {},
        }

    # -- centre de contrôle -----------------------------------------------------------
    @router.get("/activity")
    async def activity() -> Response:
        """Un seul appel pour la carte en direct : derniers paiements et webhooks reçus."""
        data = await graphql_data(gateway, queries.PAYMENTS, {"first": 20, "after": None})
        return JSONResponse(
            {
                "payments": data["payment_intents"]["nodes"],
                "webhooks": inbox.recent(20),
                "failing": inbox.failing,
            }
        )

    @router.post("/demo/payment")
    async def demo_payment(body: DemoPaymentIn) -> Response:
        """Paiement de démonstration créé puis confirmé par le serveur du marchand.

        ``suspect`` utilise le numéro de la liste noire de démonstration : refus anti-fraude.
        """
        phone = DEMO_BLOCKED_PHONE if body.kind == "suspect" else random_demo_phone()
        created = await gateway.call(
            "POST",
            "/v1/payment-intents",
            {
                "amount": random_demo_amount(),
                "currency": "XOF",
                "payer_phone": phone,
                "description": "Paiement de démonstration",
                "metadata": {"order_id": f"DEMO-{secrets.randbelow(9_000) + 1_000}"},
            },
            idempotent=True,
        )
        if created.status_code != 201:
            return relay(created)
        intent_id = created.json()["id"]
        return relay(
            await gateway.call("POST", f"/v1/payment-intents/{intent_id}/confirm", idempotent=True)
        )

    # -- grand livre (back-office) ----------------------------------------------------
    @router.get("/ledger")
    async def ledger(
        scope: Literal["merchant", "platform"] = "merchant",
        first: Annotated[int, Query(ge=1, le=100)] = 15,
        after: Annotated[str | None, Query(pattern=r"^ltx_[0-9A-Za-z]{26}$")] = None,
        reference: Annotated[str | None, Query(pattern=r"^[a-z]{1,4}_[0-9A-Za-z]{26}$")] = None,
    ) -> Response:
        params = [f"limit={first}"]
        if scope == "merchant":
            params.append(f"merchant_id={(await gateway.merchant()).merchant_id}")
        if after:
            params.append(f"starting_after={after}")
        if reference:
            params.append(f"reference={reference}")
        return relay(
            await gateway.admin("GET", "/v1/admin/ledger/transactions?" + "&".join(params))
        )

    # -- encaissement ----------------------------------------------------------------
    @router.post("/checkout")
    async def checkout(body: CheckoutIn, key: IdempotencyKey = None) -> Response:
        payload: dict[str, Any] = {
            "amount": body.amount,
            "currency": "XOF",
            "payer_phone": body.payer_phone,
            "metadata": {"order_id": body.order_id} if body.order_id else {},
        }
        if body.description:
            payload["description"] = body.description
        return relay(
            await gateway.call(
                "POST", "/v1/payment-intents", payload, idempotent=True, idempotency_key=key
            )
        )

    @router.post("/payment-intents/{intent_id}/confirm")
    async def confirm(intent_id: IntentId, key: IdempotencyKey = None) -> Response:
        return relay(
            await gateway.call(
                "POST",
                f"/v1/payment-intents/{intent_id}/confirm",
                idempotent=True,
                idempotency_key=key,
            )
        )

    @router.get("/payment-intents/{intent_id}/stream")
    async def stream(
        intent_id: IntentId,
        last_event_id: Annotated[str | None, Header(alias="Last-Event-ID", max_length=32)] = None,
    ) -> Response:
        """Relais SSE avec la clé *publique* : le flux ne nécessite aucun secret (XG-12)."""
        merchant = await gateway.merchant()
        headers = {"Authorization": f"Bearer {merchant.publishable_key}"}
        if last_event_id:
            headers["Last-Event-ID"] = last_event_id
        request = gateway.api.build_request(
            "GET",
            f"/v1/payment-intents/{intent_id}/stream",
            headers=headers,
            timeout=httpx.Timeout(10.0, read=None),
        )
        upstream = await gateway.api.send(request, stream=True)
        if upstream.status_code != 200:
            await upstream.aread()
            await upstream.aclose()
            return relay(upstream)

        async def frames() -> AsyncIterator[bytes]:
            try:
                async for chunk in upstream.aiter_raw():
                    yield chunk
            finally:
                await upstream.aclose()

        return StreamingResponse(
            frames(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # -- paiements -------------------------------------------------------------------
    @router.get("/payments")
    async def payments(
        status: IntentStatusFilter | None = None,
        operator: OperatorName | None = None,
        first: Annotated[int, Query(ge=1, le=100)] = 25,
        after: Annotated[str | None, Query(pattern=r"^pi_[0-9A-Za-z]{10,40}$")] = None,
    ) -> Response:
        variables: dict[str, Any] = {"first": first, "after": after}
        variables["status"] = status.upper() if status else None
        variables["operator"] = operator.upper() if operator else None
        return JSONResponse(await graphql_data(gateway, queries.PAYMENTS, variables))

    @router.get("/payment-intents/{intent_id}")
    async def payment(intent_id: IntentId) -> Response:
        data = await graphql_data(gateway, queries.PAYMENT, {"id": intent_id})
        if data["payment_intent"] is None:
            return JSONResponse(
                {"title": "Paiement introuvable", "status": 404, "code": "resource_not_found"},
                status_code=404,
                media_type="application/problem+json",
            )
        return JSONResponse(data["payment_intent"])

    @router.post("/payment-intents/{intent_id}/refunds")
    async def refund(intent_id: IntentId, body: RefundIn, key: IdempotencyKey = None) -> Response:
        payload: dict[str, Any] = {"payment_intent": intent_id, "amount": body.amount}
        if body.reason:
            payload["reason"] = body.reason
        return relay(
            await gateway.call(
                "POST", "/v1/refunds", payload, idempotent=True, idempotency_key=key, signed=True
            )
        )

    # -- tableau de bord et trésorerie ------------------------------------------------
    @router.get("/overview")
    async def overview(days: Annotated[int, Query(ge=1, le=31)] = 7) -> Response:
        today = dakar_today()
        variables = {
            "from": (today - dt.timedelta(days=days - 1)).isoformat(),
            "to": today.isoformat(),
        }
        data, balance = await asyncio.gather(
            graphql_data(gateway, queries.OVERVIEW, variables),
            gateway.call("GET", "/v1/balance"),
        )
        if balance.status_code != 200:
            return relay(balance)
        return JSONResponse(
            {**data, "balance": balance.json(), "period": variables, "operators": OPERATORS}
        )

    @router.get("/balance")
    async def balance() -> Response:
        return relay(await gateway.call("GET", "/v1/balance"))

    @router.post("/payouts")
    async def payout(key: IdempotencyKey = None) -> Response:
        return relay(
            await gateway.call(
                "POST", "/v1/payouts", {}, idempotent=True, idempotency_key=key, signed=True
            )
        )

    # -- webhooks --------------------------------------------------------------------
    @router.get("/webhooks")
    async def webhooks() -> dict[str, Any]:
        merchant = await gateway.merchant()
        return {
            "endpoint": merchant.public_view()["webhook_endpoint"],
            "failing": inbox.failing,
            "processed": inbox.processed_count,
            "events": inbox.recent(),
        }

    @router.post("/webhooks/failure")
    async def webhook_failure(body: FailureIn) -> dict[str, bool]:
        inbox.failing = body.enabled
        return {"failing": inbox.failing}

    @router.get("/webhook-events/{event_id}")
    async def webhook_event(event_id: EventId) -> Response:
        return relay(await gateway.call("GET", f"/v1/webhook-events/{event_id}"))

    @router.post("/webhook-events/{event_id}/replay")
    async def replay(event_id: EventId) -> Response:
        return relay(await gateway.call("POST", f"/v1/webhook-events/{event_id}/replay"))

    # -- back-office Xaalis : rapprochement, règlement, grand livre -------------------
    @router.post("/operations/reconciliation")
    async def reconcile(body: DayIn) -> Response:
        payload = {"date": body.date.isoformat(), "operators": list(OPERATORS)}
        return relay(await gateway.admin("POST", "/v1/admin/reconciliations", payload))

    @router.get("/operations/reconciliation/{day}")
    async def reconciliation_report(day: dt.date) -> Response:
        return relay(await gateway.admin("GET", f"/v1/admin/reconciliations/{day.isoformat()}"))

    @router.get("/operations/reconciliation/{day}/export")
    async def reconciliation_export(day: dt.date) -> Response:
        upstream = await gateway.admin(
            "GET", f"/v1/admin/reconciliations/{day.isoformat()}?format=csv"
        )
        if upstream.status_code != 200:
            return relay(upstream)
        return Response(
            upstream.content,
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="rapprochement-{day}.csv"'},
        )

    @router.get("/operations/anomalies/{day}")
    async def injected_anomalies(day: dt.date) -> Response:
        return relay(await gateway.sim("GET", f"/statements/{day.isoformat()}/anomalies.json"))

    @router.post("/operations/settlement")
    async def settle(body: SettlementIn) -> Response:
        payload: dict[str, Any] = {"date": body.date.isoformat()}
        if body.as_of:
            payload["as_of"] = body.as_of.isoformat()
        return relay(await gateway.admin("POST", "/v1/admin/settlements", payload))

    @router.get("/operations/ledger")
    async def ledger_health() -> Response:
        return relay(await gateway.admin("GET", "/v1/admin/ledger/health"))

    # -- simulateur d'opérateurs -----------------------------------------------------
    @router.get("/simulation")
    async def simulation() -> Response:
        return relay(await gateway.sim("GET", "/_chaos"))

    @router.post("/simulation/outage")
    async def outage(body: OutageIn) -> Response:
        payload = {"operator": body.operator, "outage_seconds": body.seconds}
        return relay(await gateway.sim("POST", "/_chaos", payload))

    @router.post("/simulation/presentation")
    async def presentation() -> Response:
        """Réglages lisibles pour une démonstration : callbacks en 1,5 s, aucun aléa."""
        response = await gateway.sim("DELETE", "/_chaos")
        for operator in OPERATORS:
            response = await gateway.sim(
                "POST",
                "/_chaos",
                {
                    "operator": operator,
                    "latency_ms": [1500, 1500],
                    "failure_rate": 0,
                    "duplicate_rate": 0,
                },
            )
        return relay(response)

    @router.delete("/simulation")
    async def reset_simulation() -> Response:
        return relay(await gateway.sim("DELETE", "/_chaos"))

    return router


def install_error_handlers(app: FastAPI) -> None:
    async def _unavailable(_: Request, exc: Exception) -> Response:
        return unavailable("Passerelle Xaalis", exc)

    async def _upstream(_: Request, exc: Exception) -> Response:
        assert isinstance(exc, UpstreamProblemError)  # noqa: S101
        return exc.response

    async def _onboarding(_: Request, exc: Exception) -> Response:
        assert isinstance(exc, OnboardingError)  # noqa: S101
        return JSONResponse(
            {
                "type": "about:blank",
                "title": "Intégration du marchand impossible",
                "status": 502,
                "detail": f"{exc.step} : HTTP {exc.response.status_code}",
                "code": "onboarding_failed",
            },
            status_code=502,
            media_type="application/problem+json",
        )

    app.add_exception_handler(GatewayUnavailableError, _unavailable)
    app.add_exception_handler(UpstreamProblemError, _upstream)
    app.add_exception_handler(OnboardingError, _onboarding)
