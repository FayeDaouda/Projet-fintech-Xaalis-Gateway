"""Traces OpenTelemetry (bonus, ADR 0014) : configuration et propagation à travers l'outbox.

Une seule trace suit un paiement d'un bout à l'autre, y compris à travers les frontières
asynchrones :

    POST /confirm (api) ─▶ outbox ─▶ worker : scoring gRPC, débit HTTP ─▶ operators
        ─▶ callback (api) ─▶ outbox ─▶ worker : webhook ─▶ merchant-demo

Les appels HTTP (httpx), gRPC, SQL et Redis sont instrumentés automatiquement ; le contexte
W3C (``traceparent``) traverse l'outbox **dans la même transaction que l'événement** : il
est stocké avec le message, puis repris par le worker (``continue_trace``).

Désactivé par défaut (tests, développement local) : l'API OpenTelemetry est alors un
no-op, sans coût. Activé par docker compose (``XAALIS_OTEL_ENABLED=true``), export OTLP
vers Jaeger.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, MutableMapping
from contextlib import contextmanager
from typing import Any, Final

from opentelemetry import propagate, trace
from opentelemetry.trace import Span, SpanKind
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

TRACER: Final = trace.get_tracer("xaalis")
_EXCLUDED_URLS: Final = "healthz,readyz,metrics,static"
_configured: set[str] = set()


class TracingSettings(BaseSettings):
    """Lue par chaque processus (api, worker, risk, simulateurs) : une seule configuration."""

    model_config = SettingsConfigDict(env_prefix="XAALIS_OTEL_", extra="ignore")

    enabled: bool = False
    exporter_endpoint: str = "http://localhost:4317"
    # Échantillonnage en pour mille (entier) : 1000 = toutes les traces.
    sample_permille: int = Field(default=1_000, ge=0, le=1_000)
    environment: str = "development"


def configure_tracing(service: str, settings: TracingSettings | None = None) -> bool:
    """Installe le fournisseur de traces et l'instrumentation automatique du processus.

    Idempotent par processus ; renvoie ``True`` si le traçage est actif.
    """
    config = settings or TracingSettings()
    if not config.enabled:
        return False
    if service in _configured:
        return True
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
    from opentelemetry.instrumentation.grpc import (
        GrpcAioInstrumentorClient,
        GrpcAioInstrumentorServer,
    )
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from opentelemetry.instrumentation.redis import RedisInstrumentor
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased

    provider = TracerProvider(
        resource=Resource.create(
            {
                "service.name": service,
                "service.namespace": "xaalis",
                "deployment.environment": config.environment,
            }
        ),
        sampler=ParentBased(TraceIdRatioBased(config.sample_permille / 1_000)),
    )
    # Export en lot, dans un thread : une panne de Jaeger ne ralentit jamais un paiement.
    provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=config.exporter_endpoint, insecure=True))
    )
    trace.set_tracer_provider(provider)
    HTTPXClientInstrumentor().instrument()
    RedisInstrumentor().instrument()
    # Classes non annotées dans la bibliothèque : seul écart au typage strict, localisé ici.
    GrpcAioInstrumentorClient().instrument()  # type: ignore[no-untyped-call]
    GrpcAioInstrumentorServer().instrument()  # type: ignore[no-untyped-call]
    _configured.add(service)
    return True


def instrument_fastapi(app: Any) -> None:
    """Spans serveur pour chaque requête (sondes et métriques exclues)."""
    if not _configured:
        return
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

    FastAPIInstrumentor.instrument_app(app, excluded_urls=_EXCLUDED_URLS)


def instrument_engine(engine: Any) -> None:
    """Un span par requête SQL (texte de la requête, sans les paramètres)."""
    if not _configured:
        return
    from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor

    SQLAlchemyInstrumentor().instrument(engine=engine.sync_engine)


def current_traceparent() -> str | None:
    """Contexte W3C courant, à stocker avec un message d'outbox (``None`` sans trace)."""
    carrier: dict[str, str] = {}
    propagate.inject(carrier)
    return carrier.get("traceparent")


@contextmanager
def continue_trace(
    name: str, traceparent: str | None, attributes: Mapping[str, Any] | None = None
) -> Iterator[Span]:
    """Reprend, côté worker, la trace de la requête qui a écrit le message."""
    context = propagate.extract({"traceparent": traceparent}) if traceparent else None
    with TRACER.start_as_current_span(
        name, context=context, kind=SpanKind.CONSUMER, attributes=dict(attributes or {})
    ) as span:
        yield span


def add_trace_ids(_: Any, __: str, event_dict: MutableMapping[str, Any]) -> Mapping[str, Any]:
    """Processeur structlog : chaque log porte ``trace_id`` / ``span_id`` (log ↔ trace)."""
    context = trace.get_current_span().get_span_context()
    if context.is_valid:
        event_dict["trace_id"] = format(context.trace_id, "032x")
        event_dict["span_id"] = format(context.span_id, "016x")
    return event_dict
