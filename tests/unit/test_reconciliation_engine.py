"""Moteur de rapprochement (lot 6) : les quatre types d'écart, et des relevés invalides."""

import pytest

from xaalis.reconciliation.engine import (
    InvalidStatementError,
    Outcome,
    ledger_frame,
    parse_statement,
    reconcile,
)

HEADER = "operator_reference,merchant_reference,amount,currency,status,occurred_at\n"


def _statement(*rows: str) -> bytes:
    return (
        HEADER + "".join(f"{row},XOF,succeeded,2026-11-26T10:00:00Z\n" for row in rows)
    ).encode()


def _ledger(*rows: tuple[str, str, int]) -> list[dict[str, object]]:
    return [
        {"operator_reference": ref, "intent_id": iid, "livemode": False, "ledger_amount": amount}
        for ref, iid, amount in rows
    ]


def _outcomes(statement: bytes, ledger: list[dict[str, object]]) -> dict[str, str]:
    result = reconcile(parse_statement(statement, operator="gopay"), ledger_frame(ledger))
    return {row["operator_reference"]: row["outcome"] for row in result.to_dicts()}


def test_every_discrepancy_type_is_detected() -> None:
    statement = _statement(
        "R1,pi_1,1000", "R2,pi_2,2000", "R2,pi_2,2000", "R3,pi_3,3500", "R9,pi_9,900"
    )
    ledger = _ledger(
        ("R1", "pi_1", 1000), ("R2", "pi_2", 2000), ("R3", "pi_3", 3000), ("R4", "pi_4", 4000)
    )
    assert _outcomes(statement, ledger) == {
        "R1": Outcome.MATCHED,
        "R2": Outcome.DUPLICATE,
        "R3": Outcome.AMOUNT_MISMATCH,
        "R4": Outcome.MISSING_IN_STATEMENT,
        "R9": Outcome.MISSING_IN_LEDGER,
    }


def test_empty_statement_marks_the_whole_ledger_missing() -> None:
    outcomes = _outcomes(HEADER.encode(), _ledger(("R1", "pi_1", 100)))
    assert outcomes == {"R1": Outcome.MISSING_IN_STATEMENT}


def test_failed_lines_are_ignored_and_other_operators_filtered() -> None:
    raw = (
        b"operator,operator_reference,amount,status\n"
        b"gopay,R1,1000,succeeded\ngopay,R2,500,failed\nndiaga,N1,700,succeeded\n"
    )
    frame = parse_statement(raw, operator="gopay")
    assert frame["operator_reference"].to_list() == ["R1"]


def test_columns_are_case_and_space_insensitive_and_optional_ones_may_be_absent() -> None:
    frame = parse_statement(b" Operator_Reference , AMOUNT \n R1 , 1000 \n", operator="gopay")
    assert frame.to_dicts() == [
        {"operator_reference": "R1", "merchant_reference": None, "amount": 1000}
    ]


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (b"operator_reference,amount\nR1,12.5\n", "entier"),  # XG-01 : jamais de flottant
        (b"operator_reference,amount\nR1,abc\n", "entier"),
        (b"reference,total\nR1,100\n", "manquantes"),
        (b"operator_reference,amount,currency\nR1,100,EUR\n", "XOF"),
        (b"\xff\xfe\x00garbage", "illisible|manquantes"),
    ],
)
def test_invalid_statements_are_rejected(raw: bytes, message: str) -> None:
    with pytest.raises(InvalidStatementError, match=message):
        parse_statement(raw, operator="gopay")


def test_oversized_statement_is_rejected() -> None:
    with pytest.raises(InvalidStatementError, match="volumineux"):
        parse_statement(b"x" * (5 * 1024 * 1024 + 1), operator="gopay")
