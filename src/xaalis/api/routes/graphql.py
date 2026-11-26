"""Point d'entrée GraphQL (lot 7) : POST /graphql, clé secrète obligatoire."""

from __future__ import annotations

import json

from fastapi import Depends, Request
from strawberry.fastapi import GraphQLRouter

from xaalis.api.auth import SecretKeyDep
from xaalis.api.problems import ProblemError
from xaalis.api.resources import ResourcesDep
from xaalis.payments.service import Scope
from xaalis.reporting.schema import ReportingContext, schema


async def get_context(principal: SecretKeyDep, resources: ResourcesDep) -> ReportingContext:
    return ReportingContext(
        Scope(principal.merchant_id, principal.livemode), resources.sessionmaker
    )


async def validate_graphql_request(request: Request) -> None:
    """Refuse en 400 (RFC 9457) une requête GraphQL mal formée, que Strawberry ignorerait.

    Exemple : un ``operationName`` qui n'est pas une chaîne est silencieusement ignoré par
    Strawberry ; le contrat publié dit le contraire, on applique donc le contrat.
    """
    if request.method != "POST":
        return
    try:
        payload = json.loads(await request.body() or b"null")
    except ValueError:
        raise ProblemError(400, "invalid_graphql_request", "Corps JSON illisible.") from None
    valid = (
        isinstance(payload, dict)
        and isinstance(payload.get("query"), str)
        and payload["query"].strip() != ""
        and isinstance(payload.get("variables", {}), dict | None)
        and isinstance(payload.get("operationName"), str | None)
    )
    if not valid:
        raise ProblemError(
            400,
            "invalid_graphql_request",
            'Attendu : {"query": chaîne non vide, "variables": objet, "operationName": chaîne}.',
        )


def build_router(*, enable_ide: bool) -> GraphQLRouter[ReportingContext, None]:
    return GraphQLRouter(
        schema,
        context_getter=get_context,
        graphql_ide="graphiql" if enable_ide else None,
        allow_queries_via_get=False,  # toujours en POST : ni cache, ni CSRF par GET
        tags=["reporting"],
        dependencies=[Depends(validate_graphql_request)],
    )
