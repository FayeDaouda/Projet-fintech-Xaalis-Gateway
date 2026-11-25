"""Machine à états des intents : test exhaustif de la table + propriété Hypothesis."""

import itertools

import pytest
from hypothesis import given
from hypothesis import strategies as st

from xaalis.payments.state_machine import (
    EXPIRABLE_STATUSES,
    TERMINAL_STATUSES,
    TRANSITIONS,
    IntentEvent,
    IntentStatus,
    InvalidTransitionError,
    next_status,
)

S, E = IntentStatus, IntentEvent

# Copie INDÉPENDANTE du schéma de l'énoncé (lot 2) : toute dérive de TRANSITIONS échoue ici.
SPECIFICATION = {
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
ALL_PAIRS = list(itertools.product(IntentStatus, IntentEvent))


def test_transition_table_matches_the_specification_exactly() -> None:
    assert dict(TRANSITIONS) == SPECIFICATION


@pytest.mark.parametrize(("status", "event"), ALL_PAIRS, ids=lambda v: str(v))
def test_every_state_event_pair_is_either_allowed_or_rejected_with_409(
    status: IntentStatus, event: IntentEvent
) -> None:
    """56 couples (8 états × 7 événements) : aucun cas n'est laissé au hasard."""
    if (status, event) in SPECIFICATION:
        assert next_status(status, event) is SPECIFICATION[(status, event)]
    else:
        with pytest.raises(InvalidTransitionError) as excinfo:
            next_status(status, event)
        assert excinfo.value.code == "invalid_transition"
        assert excinfo.value.current is status


def test_terminal_statuses() -> None:
    assert frozenset({S.FAILED, S.CANCELED, S.EXPIRED, S.REFUNDED}) == TERMINAL_STATUSES


def test_xg07_only_created_and_processing_can_expire() -> None:
    assert frozenset({S.CREATED, S.PROCESSING}) == EXPIRABLE_STATUSES


def test_transition_table_is_read_only() -> None:
    with pytest.raises(TypeError):
        TRANSITIONS[(S.FAILED, E.CONFIRM)] = S.PROCESSING  # type: ignore[index]


@given(st.lists(st.sampled_from(list(IntentEvent)), max_size=30))
def test_property_no_event_sequence_escapes_the_state_machine(events: list[IntentEvent]) -> None:
    """Propriété : quelle que soit la suite d'événements, on ne quitte jamais le graphe,
    on ne sort jamais d'un état terminal, et seules les transitions prévues ont lieu."""
    status = S.CREATED
    for event in events:
        try:
            following = next_status(status, event)
        except InvalidTransitionError:
            continue
        assert status not in TERMINAL_STATUSES
        assert (status, event) in SPECIFICATION
        assert following in IntentStatus
        status = following
