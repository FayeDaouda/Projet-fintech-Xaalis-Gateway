"""Console du marchand de démo : la clé secrète reste côté serveur, les écritures sont signées
et idempotentes, les erreurs de la passerelle sont relayées telles quelles."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from typing import Any

import httpx

from xaalis.core.signatures import sign
from xaalis_sim.merchant_demo.app import DemoSettings, create_app

SECRET_KEY = "sk_test_" + "s" * 40
PUBLISHABLE_KEY = "pk_test_" + "p" * 40
ENDPOINT_SECRET = "whsec_" + "e" * 40
Handler = Callable[[httpx.Request], httpx.Response]


class FakeGateway:
    """Passerelle factice : enregistre les requêtes reçues, répond comme le contrat."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.routes: dict[tuple[str, str], Handler] = {}
        self.merchants_created = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        key = (request.method, request.url.path)
        if key in self.routes:
            return self.routes[key](request)
        if key == ("POST", "/v1/admin/auth/token"):
            return httpx.Response(200, json={"access_token": "adm-token", "token_type": "bearer"})
        if key == ("POST", "/v1/admin/merchants"):
            self.merchants_created += 1
            return httpx.Response(
                201,
                json={
                    "id": f"m_{self.merchants_created:026d}",
                    "name": json.loads(request.content)["name"],
                    "secret_key": SECRET_KEY,
                    "publishable_key": PUBLISHABLE_KEY,
                },
            )
        if key == ("POST", "/v1/webhook-endpoints"):
            body = json.loads(request.content)
            return httpx.Response(
                201, json={"id": "we_01", "url": body["url"], "secret": ENDPOINT_SECRET}
            )
        return httpx.Response(404, json={"title": "Not Found", "status": 404})


def _client(gateway: FakeGateway, simulator: Handler | None = None) -> httpx.AsyncClient:
    app = create_app(
        DemoSettings(merchant_demo_url="http://merchant-demo:8200/webhooks"),
        api_transport=httpx.MockTransport(gateway),
        simulator_transport=httpx.MockTransport(simulator or (lambda _: httpx.Response(200))),
    )
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://console")


async def test_merchant_is_onboarded_once_and_secret_key_never_reaches_the_browser() -> None:
    gateway = FakeGateway()
    async with _client(gateway) as http:
        responses = await asyncio.gather(*(http.get("/api/session") for _ in range(5)))

    assert gateway.merchants_created == 1
    for response in responses:
        assert response.status_code == 200
        assert SECRET_KEY not in response.text
        assert ENDPOINT_SECRET not in response.text
    session = responses[0].json()
    assert session["publishable_key"] == PUBLISHABLE_KEY
    assert session["webhook_endpoint"]["url"] == "http://merchant-demo:8200/webhooks"


async def test_refund_is_signed_over_the_exact_bytes_and_keeps_the_browser_idempotency_key() -> (
    None
):
    gateway = FakeGateway()
    seen: dict[str, Any] = {}

    def refund(request: httpx.Request) -> httpx.Response:
        seen.update(headers=dict(request.headers), body=request.content)
        return httpx.Response(201, json={"id": "re_1", "amount": 3000, "status": "succeeded"})

    gateway.routes[("POST", "/v1/refunds")] = refund
    async with _client(gateway) as http:
        response = await http.post(
            "/api/payment-intents/pi_01JD8Q3W7Z4K2N6R5T9V0XBCYA/refunds",
            json={"amount": 3000},
            headers={"Idempotency-Key": "browser-key-1"},
        )

    assert response.status_code == 201
    headers = seen["headers"]
    assert headers["authorization"] == f"Bearer {SECRET_KEY}"
    assert headers["idempotency-key"] == "browser-key-1"
    timestamp = int(headers["x-xaalis-timestamp"])
    assert abs(time.time() - timestamp) < 5
    assert headers["x-xaalis-signature"] == sign(SECRET_KEY, timestamp, seen["body"])
    assert json.loads(seen["body"]) == {
        "payment_intent": "pi_01JD8Q3W7Z4K2N6R5T9V0XBCYA",
        "amount": 3000,
    }


async def test_gateway_problems_are_relayed_with_their_status_and_media_type() -> None:
    gateway = FakeGateway()
    problem = {"title": "Solde insuffisant", "status": 409, "code": "insufficient_balance"}
    gateway.routes[("POST", "/v1/payouts")] = lambda _: httpx.Response(
        409, json=problem, headers={"Content-Type": "application/problem+json"}
    )
    async with _client(gateway) as http:
        response = await http.post("/api/payouts", json={})

    assert response.status_code == 409
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json() == problem


async def test_unreachable_gateway_becomes_a_502_problem() -> None:
    def down(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connexion refusée")

    app = create_app(DemoSettings(), api_transport=httpx.MockTransport(down))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://console"
    ) as http:
        response = await http.get("/api/session")

    assert response.status_code == 502
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["code"] == "upstream_unavailable"


async def test_identifiers_are_validated_before_reaching_the_gateway() -> None:
    gateway = FakeGateway()
    async with _client(gateway) as http:
        await http.get("/api/session")
        before = len(gateway.requests)
        response = await http.get("/api/payment-intents/pi_..%2Fadmin")

    assert response.status_code in {404, 422}
    assert len(gateway.requests) == before


async def test_expired_admin_token_is_renewed_once() -> None:
    gateway = FakeGateway()
    calls = {"n": 0}

    def ledger(_: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(401, json={"title": "expired", "status": 401})
        return httpx.Response(200, json={"balanced": True})

    gateway.routes[("GET", "/v1/admin/ledger/health")] = ledger
    async with _client(gateway) as http:
        response = await http.get("/api/operations/ledger")

    assert response.status_code == 200
    logins = [r for r in gateway.requests if r.url.path == "/v1/admin/auth/token"]
    assert len(logins) == 2


async def test_graphql_views_are_normalized_to_the_rest_shape() -> None:
    gateway = FakeGateway()
    node = {"id": "pi_1", "status": "PARTIALLY_REFUNDED", "operator": "GOPAY", "amountRefunded": 1}
    gateway.routes[("POST", "/graphql")] = lambda _: httpx.Response(
        200,
        json={
            "data": {"paymentIntents": {"nodes": [node], "hasNextPage": False, "endCursor": None}}
        },
    )
    async with _client(gateway) as http:
        response = await http.get("/api/payments", params={"status": "partially_refunded"})

    assert response.status_code == 200
    page = response.json()["payment_intents"]
    assert page["nodes"] == [
        {"id": "pi_1", "status": "partially_refunded", "operator": "gopay", "amount_refunded": 1}
    ]
    sent = json.loads(next(r for r in gateway.requests if r.url.path == "/graphql").content)
    assert sent["variables"]["status"] == "PARTIALLY_REFUNDED"


async def test_graphql_errors_become_a_502_problem() -> None:
    gateway = FakeGateway()
    gateway.routes[("POST", "/graphql")] = lambda _: httpx.Response(
        200, json={"data": None, "errors": [{"message": "période limitée à 366 jours"}]}
    )
    async with _client(gateway) as http:
        response = await http.get("/api/overview")

    assert response.status_code == 502
    assert "366 jours" in response.json()["detail"]


def _webhook(event_id: str, secret: str = ENDPOINT_SECRET) -> tuple[bytes, dict[str, str]]:
    body = json.dumps(
        {
            "id": event_id,
            "type": "payment.succeeded",
            "created_at": "2026-11-26T10:00:00Z",
            "data": {"object": {"id": "pi_1", "status": "succeeded"}},
        }
    ).encode()
    ts = int(time.time())
    return body, {"X-Xaalis-Signature": f"t={ts},v1={sign(secret, ts, body)}"}


async def test_inbox_journals_each_authenticated_attempt_until_delivery() -> None:
    gateway = FakeGateway()
    async with _client(gateway) as http:
        await http.get("/api/session")  # recopie le secret de l'endpoint dans le récepteur
        await http.post("/api/webhooks/failure", json={"enabled": True})
        body, headers = _webhook("evt_01")
        for _ in range(2):
            assert (await http.post("/webhooks", content=body, headers=headers)).status_code == 503
        forged, forged_headers = _webhook("evt_forged", secret="whsec_" + "x" * 40)
        await http.post("/webhooks", content=forged, headers=forged_headers)

        await http.post("/api/webhooks/failure", json={"enabled": False})
        assert (await http.post("/webhooks", content=body, headers=headers)).json() == {
            "received": True,
            "duplicate": False,
        }
        assert (await http.post("/webhooks", content=body, headers=headers)).json()["duplicate"]
        inbox = (await http.get("/api/webhooks")).json()

    assert [e["event_id"] for e in inbox["events"]] == ["evt_01"]  # le faux n'est pas journalisé
    event = inbox["events"][0]
    assert event["processed"] is True
    assert [a["outcome"] for a in event["attempts"]] == [
        "unavailable",
        "unavailable",
        "accepted",
        "duplicate",
    ]
    assert inbox["processed"] == 1


async def test_console_page_and_assets_are_served() -> None:
    async with _client(FakeGateway()) as http:
        page = await http.get("/")
        script = await http.get("/static/js/main.js")
        style = await http.get("/static/css/app.css")

    assert page.status_code == 200
    assert "text/html" in page.headers["content-type"]
    assert '<script type="module" src="/static/js/main.js">' in page.text
    assert script.status_code == 200
    assert style.status_code == 200


async def test_presentation_mode_sets_deterministic_profiles_for_every_operator() -> None:
    received: list[tuple[str, Any]] = []

    def simulator(request: httpx.Request) -> httpx.Response:
        received.append((request.method, json.loads(request.content) if request.content else None))
        return httpx.Response(200, json={})

    async with _client(FakeGateway(), simulator) as http:
        assert (await http.post("/api/simulation/presentation")).status_code == 200

    assert received[0] == ("DELETE", None)
    assert {body["operator"] for _, body in received[1:]} == {"gopay", "teranga", "ndiaga"}
    assert all(body["failure_rate"] == 0 for _, body in received[1:])
