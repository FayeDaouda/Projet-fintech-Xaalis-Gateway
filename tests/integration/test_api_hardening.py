"""Durcissement du contrat HTTP : taille des corps (OWASP API4), 405 et OpenAPI exact."""

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from tests.integration.conftest import bearer, idem


async def test_api4_declared_oversized_body_is_rejected_with_413(
    client: httpx.AsyncClient, onboarded_merchant: dict[str, Any]
) -> None:
    response = await client.post(
        "/v1/payment-intents",
        content=b"x" * (1024 * 1024 + 1),
        headers={
            **bearer(onboarded_merchant["secret_key"]),
            **idem(),
            "Content-Type": "application/json",
        },
    )
    assert response.status_code == 413
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["code"] == "payload_too_large"


async def test_api4_streamed_body_is_cut_off_even_without_content_length(
    client: httpx.AsyncClient, onboarded_merchant: dict[str, Any]
) -> None:
    async def chunks() -> AsyncIterator[bytes]:
        for _ in range(20):
            yield b"y" * 100_000  # 2 Mo au total, sans Content-Length

    response = await client.post(
        "/v1/payment-intents",
        content=chunks(),
        headers={
            **bearer(onboarded_merchant["secret_key"]),
            **idem(),
            "Content-Type": "application/json",
        },
    )
    assert response.status_code == 413


async def test_statement_upload_route_accepts_larger_bodies(
    client: httpx.AsyncClient, admin_headers: dict[str, str]
) -> None:
    big_csv = b"operator_reference,amount\n" + b"R,1\n" * 400_000  # ≈ 1,6 Mo
    response = await client.post(
        "/v1/admin/reconciliations/statements",
        params={"date": "2030-01-01", "operator": "gopay"},
        content=big_csv,
        headers={**admin_headers, "Content-Type": "text/csv"},
    )
    assert response.status_code == 202


async def test_405_lists_every_method_of_the_path(client: httpx.AsyncClient) -> None:
    response = await client.request("OPTIONS", "/v1/admin/merchants")
    assert response.status_code == 405
    assert response.headers["Allow"] == "GET, POST"  # documentées dans l'OpenAPI


def test_openapi_documents_every_error_as_problem_json(app: FastAPI) -> None:
    schema = app.openapi()
    assert "HTTPValidationError" not in schema["components"]["schemas"]
    for path, item in schema["paths"].items():
        for method, operation in item.items():
            for code, response in operation["responses"].items():
                if int(code) >= 400 and path not in ("/readyz", "/graphql"):
                    assert list(response["content"]) == ["application/problem+json"], (
                        path,
                        method,
                        code,
                    )


@pytest.mark.parametrize(
    ("path", "method"),
    [
        ("/v1/payment-intents", "post"),
        ("/v1/payment-intents/{intent_id}/confirm", "post"),
        ("/v1/refunds", "post"),
        ("/v1/payouts", "post"),
    ],
)
def test_openapi_marks_idempotency_key_as_required(app: FastAPI, path: str, method: str) -> None:
    operation = app.openapi()["paths"][path][method]
    header = next(p for p in operation["parameters"] if p["name"] == "Idempotency-Key")
    assert header["required"] is True
    assert "x-idempotency-required" not in operation


def _json_body(payload: Any) -> bytes:
    """Échappements JSON ASCII (\\u0000, \\ud9f2) : ce qu'envoie un vrai client."""
    return json.dumps(payload).encode()


def _storable(text: str) -> str:
    return "".join("\ufffd" if c == "\x00" or "\ud800" <= c <= "\udfff" else c for c in text)


@pytest.mark.parametrize("bad", ["\u0000", "a\u0000b", "\ud9f2", "x\udc02y"])
async def test_unstorable_characters_never_cause_a_500(
    client: httpx.AsyncClient, admin_headers: dict[str, str], bad: str
) -> None:
    """Trouvé par Schemathesis : U+0000 et demi-paires UTF-16 faisaient échouer l'INSERT."""
    response = await client.post(
        "/v1/admin/merchants",
        content=_json_body({"name": f"Boutique {bad}"}),
        headers={**admin_headers, "Content-Type": "application/json"},
    )
    assert response.status_code == 201
    assert response.json()["name"] == f"Boutique {_storable(bad)}"  # longueur préservée


async def test_unstorable_characters_in_metadata_keys_and_values(
    client: httpx.AsyncClient, onboarded_merchant: dict[str, Any]
) -> None:
    from tests.integration.conftest import intent_payload

    response = await client.post(
        "/v1/payment-intents",
        content=_json_body(intent_payload(metadata={"k\u0000": "v\ud9f2"})),
        headers={
            **bearer(onboarded_merchant["secret_key"]),
            **idem(),
            "Content-Type": "application/json",
        },
    )
    assert response.status_code == 201
    assert response.json()["metadata"] == {"k\ufffd": "v\ufffd"}


@pytest.mark.parametrize(
    "payload",
    [
        {"query": "{ __typename }", "operationName": {}},
        {"query": ""},
        {"variables": {}},
        {"query": "{ __typename }", "variables": []},
        [],
    ],
    ids=["operation-name-object", "empty-query", "no-query", "variables-list", "not-an-object"],
)
async def test_malformed_graphql_requests_are_rejected_with_400(
    client: httpx.AsyncClient, onboarded_merchant: dict[str, Any], payload: Any
) -> None:
    response = await client.post(
        "/graphql", json=payload, headers=bearer(onboarded_merchant["secret_key"])
    )
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_graphql_request"


async def test_statements_path_is_not_mistaken_for_a_dated_report(
    client: httpx.AsyncClient, admin_headers: dict[str, str]
) -> None:
    response = await client.get("/v1/admin/reconciliations/statements", headers=admin_headers)
    assert response.status_code == 405
    assert response.headers["Allow"] == "POST"
