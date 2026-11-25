"""Routage par préfixe et par coût (lot 3, XG-06)."""

import pytest

from xaalis.core.operators import Operator
from xaalis.routing import catalog
from xaalis.routing.catalog import (
    CATALOG,
    NoEligibleOperatorError,
    OperatorProfile,
    eligible_operators,
    rank_operators,
)


@pytest.mark.parametrize(
    ("prefix", "expected"),
    [
        ("77", [Operator.NDIAGA, Operator.GOPAY]),  # Ndiaga 0,8 % < Go Pay 0,9 %
        ("78", [Operator.GOPAY]),
        ("76", [Operator.TERANGA]),
        ("70", [Operator.TERANGA]),
        ("75", [Operator.NDIAGA]),
    ],
)
def test_xg06_operators_are_ranked_by_cost(prefix: str, expected: list[Operator]) -> None:
    assert rank_operators(f"+221{prefix}1234567") == expected


def test_xg06_costs_match_the_specification() -> None:
    assert {op: p.cost_bps for op, p in CATALOG.items()} == {
        Operator.GOPAY: 90,
        Operator.TERANGA: 100,
        Operator.NDIAGA: 80,
    }


@pytest.mark.parametrize("prefix", ["71", "72", "79"])
def test_prefix_without_operator_is_rejected(prefix: str) -> None:
    phone = f"+221{prefix}1234567"
    assert eligible_operators(phone) == []
    with pytest.raises(NoEligibleOperatorError):
        rank_operators(phone)


def test_equal_cost_is_broken_by_best_recent_success_rate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        catalog,
        "CATALOG",
        {
            Operator.GOPAY: OperatorProfile(Operator.GOPAY, frozenset({"77"}), cost_bps=90),
            Operator.NDIAGA: OperatorProfile(Operator.NDIAGA, frozenset({"77"}), cost_bps=90),
        },
    )
    phone = "+221771234567"
    rates = {Operator.GOPAY: 990, Operator.NDIAGA: 950}
    assert rank_operators(phone, rates) == [Operator.GOPAY, Operator.NDIAGA]
    assert rank_operators(phone, {Operator.GOPAY: 900, Operator.NDIAGA: 950})[0] is Operator.NDIAGA


def test_open_breakers_are_tried_last_but_never_dropped() -> None:
    from xaalis.routing.catalog import RoutingSnapshot, plan_route

    phone = "+221771234567"
    assert plan_route(phone) == [Operator.NDIAGA, Operator.GOPAY]
    snapshot = RoutingSnapshot(open_operators=frozenset({Operator.NDIAGA}))
    assert plan_route(phone, snapshot) == [Operator.GOPAY, Operator.NDIAGA]
    all_open = RoutingSnapshot(open_operators=frozenset({Operator.NDIAGA, Operator.GOPAY}))
    assert plan_route(phone, all_open) == [Operator.NDIAGA, Operator.GOPAY]
