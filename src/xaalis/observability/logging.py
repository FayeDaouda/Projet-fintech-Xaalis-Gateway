"""Logs JSON structurés (structlog), communs à l'API, au worker et aux simulateurs.

Les logs de la bibliothèque standard (uvicorn, sqlalchemy…) passent par le même
pipeline de processeurs : une seule forme de log, une ligne JSON par événement.
Le ``request_id`` est porté par les contextvars et apparaît donc automatiquement
dans tous les logs émis pendant le traitement d'une requête ou d'une tâche.
"""

from __future__ import annotations

import logging
import sys

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
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers = [handler]
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
