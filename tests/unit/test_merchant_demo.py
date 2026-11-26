"""Le marchand de démo vérifie la signature, déduplique et gère le désordre (lot 5)."""

import json
import time

import httpx

from xaalis.core.signatures import sign
from xaalis_sim.merchant_demo.app import DemoSettings, create_app

SECRET = "whsec_demo_" + "d" * 30


def _event(event_id: str, status: str, created_at: str) -> bytes:
    return json.dumps(
        {
            "id": event_id,
            "type": f"payment.{status}",
            "created_at": created_at,
            "data": {"object": {"id": "pi_1", "status": status}},
        }
    ).encode()


async def _post(
    http: httpx.AsyncClient, body: bytes, *, secret: str = SECRET, ts: int | None = None
) -> httpx.Response:
    stamp = ts or int(time.time())
    return await http.post(
        "/webhooks",
        content=body,
        headers={"X-Xaalis-Signature": f"t={stamp},v1={sign(secret, stamp, body)}"},
    )


def _client() -> httpx.AsyncClient:
    app = create_app(DemoSettings(demo_webhook_secret=SECRET))
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://demo")


async def test_valid_event_is_accepted_and_duplicate_is_ignored() -> None:
    async with _client() as http:
        body = _event("evt_1", "succeeded", "2026-11-26T10:00:00Z")
        assert (await _post(http, body)).json() == {"received": True, "duplicate": False}
        assert (await _post(http, body)).json() == {"received": True, "duplicate": True}


async def test_bad_or_stale_signature_is_rejected() -> None:
    async with _client() as http:
        body = _event("evt_2", "succeeded", "2026-11-26T10:00:00Z")
        assert (await _post(http, body, secret="whsec_wrong_" + "x" * 30)).status_code == 400
        assert (await _post(http, body, ts=int(time.time()) - 301)).status_code == 400


async def test_out_of_order_events_never_regress_the_status() -> None:
    async with _client() as http:
        await _post(http, _event("evt_b", "succeeded", "2026-11-26T10:00:05Z"))
        await _post(http, _event("evt_a", "processing", "2026-11-26T10:00:01Z"))  # en retard
        objects = (await http.get("/events")).json()["objects"]
        assert objects["pi_1"]["status"] == "succeeded"


async def test_failure_mode_returns_503() -> None:
    async with _client() as http:
        await http.post("/_fail", json={"enabled": True})
        assert (
            await _post(http, _event("evt_3", "failed", "2026-11-26T10:00:00Z"))
        ).status_code == 503


async def test_secret_of_an_endpoint_created_later_can_be_registered() -> None:
    other = "whsec_endpoint_" + "e" * 30
    async with _client() as http:
        body = _event("evt_4", "succeeded", "2026-11-26T10:00:00Z")
        assert (await _post(http, body, secret=other)).status_code == 400
        assert (await http.post("/_secrets", json={"secret": other})).status_code == 204
        assert (await _post(http, body, secret=other)).status_code == 200
