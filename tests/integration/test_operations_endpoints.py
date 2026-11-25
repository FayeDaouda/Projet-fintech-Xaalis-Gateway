"""Sondes, métriques, propagation du request_id et format RFC 9457 des erreurs."""

import httpx
import pytest
from fastapi import FastAPI

from xaalis.api.problems import PROBLEM_CONTENT_TYPE, ProblemError


async def test_healthz_is_alive(client: httpx.AsyncClient) -> None:
    response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_readyz_checks_real_dependencies(client: httpx.AsyncClient) -> None:
    response = await client.get("/readyz")
    assert response.status_code == 200
    assert response.json() == {"status": "ready", "checks": {"database": "ok", "redis": "ok"}}


async def test_metrics_expose_route_template_not_raw_path(client: httpx.AsyncClient) -> None:
    await client.get("/healthz")
    response = await client.get("/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert 'route="/healthz"' in response.text


async def test_request_id_is_echoed_back(client: httpx.AsyncClient) -> None:
    response = await client.get("/healthz", headers={"X-Request-Id": "req_from_client_123"})
    assert response.headers["X-Request-Id"] == "req_from_client_123"


async def test_request_id_is_generated_when_absent(client: httpx.AsyncClient) -> None:
    response = await client.get("/healthz")
    assert response.headers["X-Request-Id"].startswith("req_")


async def test_unknown_route_returns_problem_details(client: httpx.AsyncClient) -> None:
    response = await client.get("/v1/nope", headers={"X-Request-Id": "req_trace_404_abc"})
    assert response.status_code == 404
    assert response.headers["content-type"] == PROBLEM_CONTENT_TYPE
    body = response.json()
    assert body["code"] == "not_found"
    assert body["type"].endswith("/problems/not-found")
    assert body["status"] == 404
    assert body["instance"] == "/v1/nope"
    assert body["request_id"] == "req_trace_404_abc"


async def test_wrong_method_returns_problem_details(client: httpx.AsyncClient) -> None:
    response = await client.delete("/healthz")
    assert response.status_code == 405
    assert response.json()["code"] == "method_not_allowed"


@pytest.fixture(scope="module")
def probe_routes(app: FastAPI) -> None:
    """Routes de test qui déclenchent chaque famille d'erreur."""

    @app.get("/__test/problem", include_in_schema=False)
    async def raise_problem() -> None:
        raise ProblemError(409, "invalid_transition", "created → refunded n'est pas permis")

    @app.get("/__test/crash", include_in_schema=False)
    async def crash() -> None:
        raise RuntimeError("secret internal detail")

    @app.get("/__test/validated", include_in_schema=False)
    async def validated(limit: int) -> dict[str, int]:
        return {"limit": limit}


@pytest.mark.usefixtures("probe_routes")
async def test_problem_error_is_rendered_with_stable_code(client: httpx.AsyncClient) -> None:
    response = await client.get("/__test/problem")
    assert response.status_code == 409
    body = response.json()
    assert body["code"] == "invalid_transition"
    assert body["type"].endswith("/problems/invalid-transition")
    assert body["title"] == "Conflict"
    assert "refunded" in body["detail"]


@pytest.mark.usefixtures("probe_routes")
async def test_validation_error_lists_fields(client: httpx.AsyncClient) -> None:
    response = await client.get("/__test/validated", params={"limit": "abc"})
    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "validation_error"
    assert body["errors"][0]["loc"] == ["query", "limit"]


@pytest.mark.usefixtures("probe_routes")
async def test_unhandled_error_does_not_leak_internals(app: FastAPI) -> None:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
        response = await http.get("/__test/crash")
    assert response.status_code == 500
    assert response.json()["code"] == "internal_error"
    assert "secret internal detail" not in response.text
