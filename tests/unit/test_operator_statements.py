"""Relevés du simulateur : anomalies injectées de façon déterministe (lot 6)."""

from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from xaalis.reconciliation.engine import parse_statement
from xaalis_sim.operators.statements import build_statement

DAY = date(2026, 11, 26)


@dataclass
class FakeCharge:
    operator: str
    reference: str
    operator_reference: str
    amount: int
    status: str = "succeeded"
    completed_at: Any = datetime(2026, 11, 26, 10, tzinfo=UTC)


def _charges(n: int, operator: str = "gopay") -> list[FakeCharge]:
    return [FakeCharge(operator, f"pi_{i}", f"GP-{i}", 1_000 * (i + 1)) for i in range(n)]


def test_all_four_anomalies_are_injected_and_listed() -> None:
    statement = build_statement(_charges(6), ["gopay"], DAY, inject=True)
    assert sorted(a["type"] for a in statement.anomalies) == [
        "amount",
        "duplicate",
        "extra",
        "missing",
    ]
    lines = parse_statement(statement.csv.encode(), operator="gopay")
    assert lines.height == 6 - 1 + 1 + 1  # − manquante + doublon + inventée


def test_statement_is_deterministic_for_a_given_day() -> None:
    first = build_statement(_charges(6), ["gopay"], DAY, inject=True)
    second = build_statement(_charges(6), ["gopay"], DAY, inject=True)
    assert first.csv == second.csv
    assert first.anomalies == second.anomalies


def test_clean_statement_when_anomalies_are_disabled() -> None:
    statement = build_statement(_charges(3), ["gopay"], DAY, inject=False)
    assert statement.anomalies == []
    assert parse_statement(statement.csv.encode(), operator="gopay").height == 3


def test_only_successful_charges_of_the_day_and_operator_are_listed() -> None:
    charges = [
        *_charges(2),
        FakeCharge("gopay", "pi_f", "GP-F", 500, status="failed"),
        FakeCharge(
            "gopay", "pi_y", "GP-Y", 500, completed_at=datetime(2026, 11, 25, 23, tzinfo=UTC)
        ),
        FakeCharge("ndiaga", "pi_n", "NC-1", 500),
    ]
    statement = build_statement(charges, ["gopay"], DAY, inject=False)
    refs = parse_statement(statement.csv.encode(), operator="gopay")["operator_reference"].to_list()
    assert sorted(refs) == ["GP-0", "GP-1"]


def test_few_charges_still_get_an_extra_line() -> None:
    statement = build_statement([], ["teranga"], DAY, inject=True)
    assert [a["type"] for a in statement.anomalies] == ["extra"]
