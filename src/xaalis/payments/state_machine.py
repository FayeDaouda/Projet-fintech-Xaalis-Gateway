"""Machine à états d'un payment intent — **seul** endroit où les transitions sont définies.

::

    created             ── confirm ────────────▶ processing
    created             ── cancel ─────────────▶ canceled
    created             ── expire (15 min) ────▶ expired
    processing          ── operator_succeeded ─▶ succeeded
    processing          ── operator_failed ────▶ failed
    processing          ── expire (15 min) ────▶ expired
    succeeded           ── refund_partial ─────▶ partially_refunded
    succeeded           ── refund_full ────────▶ refunded
    partially_refunded  ── refund_partial ─────▶ partially_refunded
    partially_refunded  ── refund_full ────────▶ refunded

Toute transition absente de ``TRANSITIONS`` est refusée (409 ``invalid_transition``).
La table est testée de façon exhaustive : chaque couple (état, événement) est vérifié.
"""

from __future__ import annotations

from enum import StrEnum
from types import MappingProxyType
from typing import Final

from xaalis.core.errors import DomainError


class IntentStatus(StrEnum):
    CREATED = "created"
    PROCESSING = "processing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELED = "canceled"
    EXPIRED = "expired"
    PARTIALLY_REFUNDED = "partially_refunded"
    REFUNDED = "refunded"


class IntentEvent(StrEnum):
    CONFIRM = "confirm"
    CANCEL = "cancel"
    EXPIRE = "expire"
    OPERATOR_SUCCEEDED = "operator_succeeded"
    OPERATOR_FAILED = "operator_failed"
    REFUND_PARTIAL = "refund_partial"
    REFUND_FULL = "refund_full"


S, E = IntentStatus, IntentEvent

TRANSITIONS: Final = MappingProxyType(
    {
        (S.CREATED, E.CONFIRM): S.PROCESSING,
        (S.CREATED, E.CANCEL): S.CANCELED,
        (S.CREATED, E.EXPIRE): S.EXPIRED,
        (S.PROCESSING, E.OPERATOR_SUCCEEDED): S.SUCCEEDED,
        (S.PROCESSING, E.OPERATOR_FAILED): S.FAILED,
        (S.PROCESSING, E.EXPIRE): S.EXPIRED,
        (S.SUCCEEDED, E.REFUND_PARTIAL): S.PARTIALLY_REFUNDED,
        (S.SUCCEEDED, E.REFUND_FULL): S.REFUNDED,
        (S.PARTIALLY_REFUNDED, E.REFUND_PARTIAL): S.PARTIALLY_REFUNDED,
        (S.PARTIALLY_REFUNDED, E.REFUND_FULL): S.REFUNDED,
    }
)

TERMINAL_STATUSES: Final = frozenset(
    status for status in IntentStatus if not any(src is status for src, _ in TRANSITIONS)
)
# États qui expirent au bout de 15 minutes sans issue finale (XG-07).
EXPIRABLE_STATUSES: Final = frozenset(
    src for (src, event) in TRANSITIONS if event is IntentEvent.EXPIRE
)


class InvalidTransitionError(DomainError):
    code = "invalid_transition"

    def __init__(self, current: IntentStatus, event: IntentEvent) -> None:
        super().__init__(f"transition « {event} » impossible depuis l'état « {current} »")
        self.current = current
        self.event = event


def next_status(current: IntentStatus, event: IntentEvent) -> IntentStatus:
    try:
        return TRANSITIONS[(current, event)]
    except KeyError:
        raise InvalidTransitionError(current, event) from None
