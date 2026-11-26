"""Bonus OpenTelemetry (ADR 0014) : la trace d'une requête traverse l'outbox jusqu'au worker."""

from __future__ import annotations

import re
import subprocess
import sys
from collections.abc import Iterator

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind

from xaalis.observability.tracing import (
    TRACER,
    TracingSettings,
    add_trace_ids,
    configure_tracing,
    continue_trace,
    current_traceparent,
)

TRACEPARENT = re.compile(r"^00-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}$")  # W3C Trace Context
_EXPORTER = InMemorySpanExporter()
_attached = False


@pytest.fixture
def spans() -> Iterator[InMemorySpanExporter]:
    """Exporteur en mémoire branché sur LE fournisseur global du processus : l'API n'en
    autorise qu'un, et un autre test a pu l'installer avant celui-ci (ordre indifférent)."""
    global _attached  # branchement unique pour tout le processus de test
    if not isinstance(trace.get_tracer_provider(), TracerProvider):
        trace.set_tracer_provider(TracerProvider())
    provider = trace.get_tracer_provider()
    assert isinstance(provider, TracerProvider)
    if not _attached:
        provider.add_span_processor(SimpleSpanProcessor(_EXPORTER))
        _attached = True
    _EXPORTER.clear()
    yield _EXPORTER
    _EXPORTER.clear()


def test_tracing_is_a_no_op_unless_enabled() -> None:
    assert configure_tracing("xaalis-test", TracingSettings(enabled=False)) is False


def test_without_an_active_span_there_is_nothing_to_propagate() -> None:
    assert current_traceparent() is None


def test_worker_span_continues_the_trace_written_with_the_outbox_message(
    spans: InMemorySpanExporter,
) -> None:
    with TRACER.start_as_current_span("POST /confirm", kind=SpanKind.SERVER) as request:
        stored = current_traceparent()  # ce que enqueue() écrit avec le message
    assert stored is not None
    assert TRACEPARENT.match(stored)

    with continue_trace("operator charge", stored, {"xaalis.intent_id": "pi_1"}) as worker:
        pass

    parent = request.get_span_context()
    child = worker.get_span_context()
    assert child.trace_id == parent.trace_id  # une seule trace, de l'API au worker
    finished = {span.name: span for span in spans.get_finished_spans()}
    consumer = finished["operator charge"]
    assert consumer.parent is not None
    assert consumer.parent.span_id == parent.span_id
    assert consumer.kind is SpanKind.CONSUMER
    assert consumer.attributes == {"xaalis.intent_id": "pi_1"}


def test_message_without_trace_starts_a_new_root_span(spans: InMemorySpanExporter) -> None:
    with continue_trace("webhook delivery", None) as span:
        assert span.get_span_context().is_valid
    (finished,) = spans.get_finished_spans()
    assert finished.parent is None


def test_logs_carry_the_ids_of_the_current_trace(spans: InMemorySpanExporter) -> None:
    assert "trace_id" not in add_trace_ids(None, "info", {"event": "hors trace"})
    with TRACER.start_as_current_span("requête") as span:
        event = add_trace_ids(None, "info", {"event": "dans la trace"})
    context = span.get_span_context()
    assert event["trace_id"] == format(context.trace_id, "032x")
    assert event["span_id"] == format(context.span_id, "016x")


def test_enabling_tracing_installs_the_sdk_and_never_blocks_without_a_collector() -> None:
    """Dans un processus séparé (le fournisseur global ne se remplace pas) : traçage actif,
    collecteur injoignable, un span créé et la sortie reste immédiate (export asynchrone)."""
    script = (
        "from xaalis.observability.tracing import TRACER, TracingSettings, configure_tracing;"
        "s = TracingSettings(enabled=True, exporter_endpoint='http://127.0.0.1:9');"
        "assert configure_tracing('xaalis-test', s) is True;"
        "assert configure_tracing('xaalis-test', s) is True;"
        "span = TRACER.start_span('essai'); span.end();"
        "assert span.get_span_context().is_valid"
    )
    result = subprocess.run(  # noqa: S603 — commande fixe, interpréteur courant
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=60, check=False
    )
    assert result.returncode == 0, result.stderr
