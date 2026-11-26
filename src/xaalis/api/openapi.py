"""Contrat OpenAPI exact (vérifié par Schemathesis).

FastAPI documente ses erreurs de validation en ``application/json`` avec son propre schéma
(``HTTPValidationError``), alors que l'API renvoie **toujours** du ``application/problem+json``
(RFC 9457). Ce module post-traite le schéma généré, une fois pour toutes les routes :

- toute réponse d'erreur documentée devient ``application/problem+json`` → ``Problem`` ;
- les erreurs transverses que chaque route peut produire sont déclarées : 422 (validation),
  413 (corps trop grand, routes à corps), 401/403/429 (routes authentifiées), 503
  (contention passagère, routes d'écriture), 500 ;
- les schémas de validation propres à FastAPI, jamais renvoyés, sont retirés.
"""

from __future__ import annotations

from typing import Any, Final

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi

from xaalis.api.problems import FieldError, Problem

_PROBLEM_REF: Final = {"$ref": "#/components/schemas/Problem"}
_PROBLEM_CONTENT: Final = {"application/problem+json": {"schema": _PROBLEM_REF}}
_FASTAPI_ONLY: Final = ("HTTPValidationError", "ValidationError")
_DESCRIPTIONS: Final = {
    "400": "Corps de requête illisible.",
    "401": "Authentification absente ou invalide.",
    "403": "Authentifié, mais non autorisé.",
    "413": "Corps de requête trop volumineux.",
    "422": "Requête invalide (validation).",
    "429": "Limite de débit atteinte (XG-13) ; voir Retry-After.",
    "500": "Erreur interne (sans détail technique).",
    "503": "Ressource momentanément verrouillée ; voir Retry-After.",
}
_WRITE_METHODS: Final = frozenset({"post", "put", "patch", "delete"})


def _is_problem(response: dict[str, Any]) -> bool:
    content = response.get("content")
    if not content:
        return True
    refs = {str(media.get("schema", {}).get("$ref", "")) for media in content.values()}
    return any(ref.endswith(("/Problem", "/HTTPValidationError")) for ref in refs)


def _require_idempotency_key(operation: dict[str, Any]) -> None:
    if operation.pop("x-idempotency-required", False):
        for parameter in operation.get("parameters", []):
            if parameter.get("in") == "header" and parameter.get("name") == "Idempotency-Key":
                parameter["required"] = True
        operation.setdefault("responses", {}).setdefault(
            "400", {"description": "En-tête Idempotency-Key manquant."}
        )


def _document_graphql(paths: dict[str, Any]) -> None:
    """Routes générées par Strawberry : on décrit ce qu'elles renvoient réellement."""
    item = paths.get("/graphql", {})
    if "get" in item:  # GraphiQL (hors production)
        item["get"]["responses"] = {
            "200": {"description": "Interface GraphiQL.", "content": {"text/html": {}}},
            **{
                code: {"description": _DESCRIPTIONS[code], "content": dict(_PROBLEM_CONTENT)}
                for code in ("401", "403", "429")
            },
        }
    if "post" in item:
        item["post"]["requestBody"] = {
            "required": True,
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "required": ["query"],
                        "properties": {
                            "query": {"type": "string", "minLength": 1, "pattern": "\\S"},
                            "variables": {"type": ["object", "null"]},
                            "operationName": {"type": ["string", "null"]},
                        },
                    }
                }
            },
        }
        responses = item["post"].setdefault("responses", {})
        responses["400"] = {
            "description": "Requête GraphQL mal formée.",
            "content": dict(_PROBLEM_CONTENT),
        }


def _normalize(operation: dict[str, Any], method: str) -> None:
    _require_idempotency_key(operation)
    responses: dict[str, Any] = operation.setdefault("responses", {})
    expected = {"422", "500"}
    if operation.get("security"):
        expected |= {"401", "403", "429"}
    if "requestBody" in operation:
        expected |= {"400", "413"}
    if method in _WRITE_METHODS:
        expected.add("503")
    for code in expected:
        responses.setdefault(code, {"description": _DESCRIPTIONS[code]})
    for code, response in responses.items():
        if code.isdigit() and int(code) >= 400 and _is_problem(response):
            response["content"] = dict(_PROBLEM_CONTENT)


_BOUND_KEYS: Final = ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum")


def _integral_bounds(node: Any) -> None:
    """FastAPI publie ``minimum: 100.0`` : on rend les bornes entières (montants en XOF)."""
    if isinstance(node, dict):
        for key in _BOUND_KEYS:
            value = node.get(key)
            # Les flottants viennent du schéma généré par FastAPI, jamais d'un montant.
            if isinstance(value, float) and value.is_integer():
                node[key] = int(value)
        for child in node.values():
            _integral_bounds(child)
    elif isinstance(node, list):
        for child in node:
            _integral_bounds(child)


def install_openapi(app: FastAPI) -> None:
    def build() -> dict[str, Any]:
        if app.openapi_schema:
            return app.openapi_schema
        schema = get_openapi(
            title=app.title,
            version=app.version,
            summary=app.summary,
            description=app.description,
            routes=app.routes,
        )
        components = schema.setdefault("components", {}).setdefault("schemas", {})
        for name in _FASTAPI_ONLY:
            components.pop(name, None)
        components["Problem"] = Problem.model_json_schema(
            ref_template="#/components/schemas/{model}"
        )
        components["Problem"].pop("$defs", None)
        components["FieldError"] = FieldError.model_json_schema()
        for path_item in schema.get("paths", {}).values():
            for method, operation in path_item.items():
                if isinstance(operation, dict) and method in {"get", *_WRITE_METHODS}:
                    _normalize(operation, method)
        _document_graphql(schema.get("paths", {}))
        _integral_bounds(schema)
        app.openapi_schema = schema
        return schema

    app.openapi = build  # type: ignore[method-assign]
