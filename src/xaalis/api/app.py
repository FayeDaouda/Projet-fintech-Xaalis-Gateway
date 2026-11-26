"""Fabrique de l'application FastAPI publique (REST, puis GraphQL et SSE).

Lancement : ``uvicorn xaalis.api.app:create_app --factory``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

import xaalis.models  # noqa: F401 — enregistre TOUS les modèles ORM (clés étrangères)
from xaalis import __version__
from xaalis.api.middleware import RequestContextMiddleware
from xaalis.api.problems import install_problem_handlers
from xaalis.api.resources import Resources
from xaalis.api.routes import (
    admin,
    api_keys,
    graphql,
    health,
    operators,
    payment_intents,
    payouts,
    reconciliation,
    refunds,
    webhooks,
)
from xaalis.config import Settings, get_settings
from xaalis.observability.logging import configure_logging, get_logger
from xaalis.realtime.broker import EventBroker

_log = get_logger(__name__)


def create_app(settings: Settings | None = None, resources: Resources | None = None) -> FastAPI:
    """Construit l'application.

    ``resources`` permet aux tests d'injecter des ressources déjà ouvertes ; elles
    restent alors sous la responsabilité de l'appelant et ne sont pas fermées ici.
    """
    settings = settings or (resources.settings if resources else get_settings())
    configure_logging(
        service=settings.service_name, level=settings.log_level, json_output=settings.log_json
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        owned = resources is None
        app.state.resources = resources or Resources.from_settings(settings)
        app.state.broker = EventBroker(settings.database_url)
        await app.state.broker.start()
        _log.info("api_started", environment=settings.environment, version=__version__)
        try:
            yield
        finally:
            await app.state.broker.stop()
            if owned:
                await app.state.resources.aclose()
            _log.info("api_stopped")

    app = FastAPI(
        title="Xaalis Gateway API",
        version=__version__,
        summary="Passerelle de paiement mobile money multi-opérateurs (Go Pay, Teranga Money, "
        "Ndiaga Cash).",
        lifespan=lifespan,
    )
    app.add_middleware(RequestContextMiddleware)
    install_problem_handlers(app)
    app.include_router(health.router)
    app.include_router(admin.auth_router)
    app.include_router(admin.router)
    app.include_router(reconciliation.router)
    app.include_router(api_keys.router)
    app.include_router(payment_intents.router)
    app.include_router(operators.router)
    app.include_router(refunds.router)
    app.include_router(payouts.router)
    app.include_router(webhooks.router)
    app.include_router(
        graphql.build_router(enable_ide=settings.environment != "production"), prefix="/graphql"
    )
    return app
