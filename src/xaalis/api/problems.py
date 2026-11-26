"""Erreurs HTTP au format RFC 9457 (``application/problem+json``).

Chaque problème porte un ``code`` stable, lisible par machine, dont dérive l'URI
``type`` : ``invalid_transition`` → ``https://docs.xaalis.example/problems/invalid-transition``.
Toute réponse d'erreur de l'API, y compris 404/405/422/500 produits par le framework,
passe par ce module : le client n'a qu'un seul format à gérer.
"""

from __future__ import annotations

from collections.abc import Mapping
from http import HTTPStatus
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import DBAPIError
from starlette.exceptions import HTTPException as StarletteHTTPException

from xaalis.core.errors import DomainError
from xaalis.observability.logging import get_logger

PROBLEM_CONTENT_TYPE = "application/problem+json"
PROBLEM_TYPE_BASE = "https://docs.xaalis.example/problems/"

_log = get_logger(__name__)


class FieldError(BaseModel):
    model_config = ConfigDict(extra="forbid")

    loc: list[str | int] = Field(description="Chemin du champ en erreur.")
    msg: str
    type: str


class Problem(BaseModel):
    """Corps d'une réponse d'erreur (RFC 9457)."""

    model_config = ConfigDict(extra="forbid")

    type: str = Field(examples=[f"{PROBLEM_TYPE_BASE}invalid-transition"])
    title: str
    status: int
    detail: str | None = None
    instance: str | None = None
    code: str = Field(description="Code d'erreur stable.", examples=["invalid_transition"])
    request_id: str | None = None
    errors: list[FieldError] | None = None


def problem_type(code: str) -> str:
    return PROBLEM_TYPE_BASE + code.replace("_", "-")


class ProblemError(Exception):
    """Exception levée par la couche API ; convertie en réponse RFC 9457."""

    def __init__(
        self,
        status: int,
        code: str,
        detail: str | None = None,
        *,
        title: str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(detail or code)
        self.status = status
        self.code = code
        self.detail = detail
        self.title = title or HTTPStatus(status).phrase
        self.headers = dict(headers or {})


def problem_response(
    request: Request,
    *,
    status: int,
    code: str,
    title: str | None = None,
    detail: str | None = None,
    errors: list[FieldError] | None = None,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    body = Problem(
        type=problem_type(code),
        title=title or HTTPStatus(status).phrase,
        status=status,
        detail=detail,
        instance=request.url.path,
        code=code,
        request_id=getattr(request.state, "request_id", None),
        errors=errors,
    )
    return JSONResponse(
        body.model_dump(mode="json", exclude_none=True),
        status_code=status,
        media_type=PROBLEM_CONTENT_TYPE,
        headers=dict(headers or {}),
    )


_STATUS_CODES: Mapping[int, str] = {
    400: "bad_request",
    401: "unauthenticated",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    406: "not_acceptable",
    409: "conflict",
    413: "payload_too_large",
    415: "unsupported_media_type",
    429: "rate_limited",
}


# Table unique de traduction des erreurs métier en statuts HTTP.
DOMAIN_ERROR_STATUS: Mapping[str, int] = {
    "not_found": 404,
    "invalid_transition": 409,
    "intent_expired": 409,
    "payer_processing_limit": 409,
    "idempotency_key_reused": 422,
    "idempotency_key_in_progress": 409,
    "invalid_phone": 422,
    "invalid_amount": 422,
    "unsupported_currency": 422,
    "no_eligible_operator": 422,
    "invalid_signature": 401,
    "malformed_callback": 400,
    "refund_window_expired": 409,
    "refund_exceeds_amount": 409,
    "insufficient_balance": 409,
    "unsafe_webhook_url": 422,
}


async def _on_domain_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, DomainError)  # noqa: S101
    status = DOMAIN_ERROR_STATUS.get(exc.code)
    if status is None:
        _log.warning("unmapped_domain_error", code=exc.code)
        status = 400
    return problem_response(request, status=status, code=exc.code, detail=exc.detail)


async def _on_problem(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, ProblemError)  # noqa: S101 — garanti par l'enregistrement du handler
    return problem_response(
        request,
        status=exc.status,
        code=exc.code,
        title=exc.title,
        detail=exc.detail,
        headers=exc.headers,
    )


async def _on_http_exception(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StarletteHTTPException)  # noqa: S101
    detail = exc.detail if isinstance(exc.detail, str) else None
    return problem_response(
        request,
        status=exc.status_code,
        code=_STATUS_CODES.get(exc.status_code, "http_error"),
        detail=detail if detail != HTTPStatus(exc.status_code).phrase else None,
        headers=exc.headers,
    )


async def _on_validation_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)  # noqa: S101
    errors = [_to_field_error(err) for err in exc.errors()]
    return problem_response(
        request,
        status=422,
        code="validation_error",
        detail="La requête ne respecte pas le schéma attendu.",
        errors=errors,
    )


# Contention passagère (ADR 0002) : le client peut simplement réessayer.
_RETRYABLE_SQLSTATES: Mapping[str, str] = {
    "55P03": "lock_timeout",  # lock_timeout dépassé
    "40P01": "deadlock_detected",
    "40001": "serialization_failure",
}


async def _on_database_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, DBAPIError)  # noqa: S101
    state = getattr(exc.orig, "sqlstate", None) or getattr(exc.orig, "pgcode", None)
    code = _RETRYABLE_SQLSTATES.get(str(state))
    if code is None:
        return await _on_unhandled(request, exc)
    _log.warning("database_contention", sqlstate=state)
    return problem_response(
        request,
        status=503,
        code=code,
        detail="Ressource momentanément verrouillée, réessayez.",
        headers={"Retry-After": "1"},
    )


async def _on_unhandled(request: Request, exc: Exception) -> JSONResponse:
    # Le détail technique va dans les logs, jamais dans la réponse (OWASP API3/API8).
    _log.exception("unhandled_exception", exc_type=type(exc).__name__)
    return problem_response(request, status=500, code="internal_error")


def _to_field_error(err: Mapping[str, Any]) -> FieldError:
    loc = [part if isinstance(part, int) else str(part) for part in err.get("loc", ())]
    return FieldError(loc=loc, msg=str(err.get("msg", "")), type=str(err.get("type", "")))


def install_problem_handlers(app: FastAPI) -> None:
    app.add_exception_handler(ProblemError, _on_problem)
    app.add_exception_handler(DomainError, _on_domain_error)
    app.add_exception_handler(DBAPIError, _on_database_error)
    app.add_exception_handler(StarletteHTTPException, _on_http_exception)
    app.add_exception_handler(RequestValidationError, _on_validation_error)
    app.add_exception_handler(Exception, _on_unhandled)
