"""Point d'entrée GraphQL (lot 7) : POST /graphql, clé secrète obligatoire."""

from __future__ import annotations

from strawberry.fastapi import GraphQLRouter

from xaalis.api.auth import SecretKeyDep
from xaalis.api.resources import ResourcesDep
from xaalis.payments.service import Scope
from xaalis.reporting.schema import ReportingContext, schema


async def get_context(principal: SecretKeyDep, resources: ResourcesDep) -> ReportingContext:
    return ReportingContext(
        Scope(principal.merchant_id, principal.livemode), resources.sessionmaker
    )


def build_router(*, enable_ide: bool) -> GraphQLRouter[ReportingContext, None]:
    return GraphQLRouter(
        schema,
        context_getter=get_context,
        graphql_ide="graphiql" if enable_ide else None,
        allow_queries_via_get=False,  # toujours en POST : ni cache, ni CSRF par GET
        tags=["reporting"],
    )
