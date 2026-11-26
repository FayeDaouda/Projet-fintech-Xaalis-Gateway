"""Logs JSON structurés (structlog), communs à l'API, au worker et aux simulateurs.

Les logs de la bibliothèque standard (uvicorn, sqlalchemy…) passent par le même
pipeline de processeurs : une seule forme de log, une ligne JSON par événement.

**Non bloquant** : la boucle asyncio ne fait que déposer l'enregistrement dans une file
(``QueueHandler``) ; un thread dédié (``QueueListener``) le formate et l'écrit sur stdout.
Sous Docker, stdout est un tube vers le démon de journalisation : écrire directement depuis
la boucle la gelait dès que le tube saturait (médiane 400 ms → 110 ms, docs/load-test.md).
Le ``request_id`` est porté par les contextvars et apparaît donc automatiquement
dans tous les logs émis pendant le traitement d'une requête ou d'une tâche.
"""

from __future__ import annotations

import atexit
import logging
import logging.handlers
import queue
import sys
from typing import Any

import structlog
from structlog.typing import EventDict, Processor, WrappedLogger

from xaalis.core.phone import mask_phones_in_text


def _service_tagger(service: str) -> Processor:
    def add_service(_: WrappedLogger, __: str, event_dict: EventDict) -> EventDict:
        event_dict.setdefault("service", service)
        return event_dict

    return add_service


def _scrub(value: object) -> object:
    if isinstance(value, str):
        return mask_phones_in_text(value)
    if isinstance(value, dict):
        return {key: _scrub(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_scrub(item) for item in value]
    return value


def mask_phone_numbers(_: WrappedLogger, __: str, event_dict: EventDict) -> EventDict:
    """XG-03 : aucun numéro de payeur en clair dans les logs, quel que soit le champ."""
    return {key: _scrub(value) for key, value in event_dict.items()}


class _NonBlockingHandler(logging.handlers.QueueHandler):
    """Dépose l'enregistrement tel quel : le formatage structlog se fait dans le thread
    d'écriture (``prepare`` par défaut formaterait dans la boucle et perdrait le JSON)."""

    def prepare(self, record: logging.LogRecord) -> logging.LogRecord:
        return record


class _StdoutHandler(logging.StreamHandler):  # type: ignore[type-arg]
    """Écrit sur le ``sys.stdout`` courant, résolu à chaque écriture (pas à la création)."""

    @property
    def stream(self) -> Any:
        return sys.stdout

    @stream.setter
    def stream(self, _: Any) -> None:
        pass


_listener: logging.handlers.QueueListener | None = None


def _stop_listener() -> None:
    """Vide la file puis arrête l'écrivain (idempotent)."""
    global _listener
    if _listener is not None and getattr(_listener, "_thread", None) is not None:
        _listener.stop()
    _listener = None


def flush_logs() -> None:
    """Attend que tous les enregistrements en file soient écrits (tests, arrêt propre)."""
    listener = _listener
    if listener is not None and getattr(listener, "_thread", None) is not None:
        listener.stop()
        listener.start()


atexit.register(_stop_listener)


def configure_logging(*, service: str, level: str = "INFO", json_output: bool = True) -> None:
    shared: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        _service_tagger(service),
        mask_phone_numbers,
    ]
    renderer: Processor = (
        structlog.processors.JSONRenderer() if json_output else structlog.dev.ConsoleRenderer()
    )

    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.format_exc_info,
            renderer,
        ],
    )
    handler = _StdoutHandler()
    handler.setFormatter(formatter)

    global _listener  # un seul écrivain par processus
    _stop_listener()
    log_queue: queue.SimpleQueue[logging.LogRecord] = queue.SimpleQueue()
    _listener = logging.handlers.QueueListener(log_queue, handler, respect_handler_level=True)
    _listener.start()

    root = logging.getLogger()
    root.handlers = [_NonBlockingHandler(log_queue)]
    root.setLevel(level)

    # uvicorn installe ses propres handlers : on les retire pour garder un format unique.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers = []
        uvicorn_logger.propagate = True
    # Le journal d'accès est produit par notre middleware (avec request_id et durée).
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    logger: structlog.stdlib.BoundLogger = structlog.stdlib.get_logger(name)
    return logger
