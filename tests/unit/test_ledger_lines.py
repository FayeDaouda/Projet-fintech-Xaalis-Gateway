"""Lignes d'écriture et schémas comptables : validation pure de l'invariant XG-16."""

import pytest
from hypothesis import given
from hypothesis import strategies as st

from xaalis.core.fees import fee_breakdown
from xaalis.core.money import MAX_INTENT_AMOUNT, MIN_INTENT_AMOUNT
from xaalis.core.operators import Operator
from xaalis.ledger import recipes
from xaalis.ledger.accounts import AccountKind, AccountRef
from xaalis.ledger.lines import Line, UnbalancedPostingError, credit, debit, validate_lines

MERCHANT = "m_TEST"
amounts = st.integers(min_value=1, max_value=10**12)
operators = st.sampled_from(list(Operator))


def test_xg16_debit_is_positive_and_credit_negative() -> None:
    account = AccountRef.revenue()
    assert debit(account, 500).amount == 500
    assert credit(account, 500).amount == -500


@pytest.mark.parametrize("amount", [0, -10])
def test_xg16_debit_and_credit_require_strictly_positive_amounts(amount: int) -> None:
    with pytest.raises(UnbalancedPostingError):
        debit(AccountRef.revenue(), amount)
    with pytest.raises(UnbalancedPostingError):
        credit(AccountRef.revenue(), amount)


def test_xg16_single_line_is_rejected() -> None:
    with pytest.raises(UnbalancedPostingError, match="au moins deux"):
        validate_lines([debit(AccountRef.revenue(), 10)])


def test_xg16_zero_line_is_rejected() -> None:
    lines = [Line(AccountRef.revenue(), 0), Line(AccountRef.merchant_pending(MERCHANT), 0)]
    with pytest.raises(UnbalancedPostingError, match="nulle"):
        validate_lines(lines)


def test_xg16_spec_example_capture_of_10000() -> None:
    lines = recipes.payment_capture(MERCHANT, Operator.GOPAY, fee_breakdown(10_000))
    assert {(line.account.code, line.amount) for line in lines} == {
        ("operator_clearing:gopay", 10_000),
        (f"merchant_pending:{MERCHANT}", -9_820),
        ("xaalis_revenue", -180),
    }


def test_xg05_capture_of_minimum_amount_has_no_zero_merchant_line() -> None:
    lines = recipes.payment_capture(MERCHANT, Operator.NDIAGA, fee_breakdown(MIN_INTENT_AMOUNT))
    validate_lines(lines)
    assert [line.account.kind for line in lines] == [
        AccountKind.OPERATOR_CLEARING,
        AccountKind.XAALIS_REVENUE,
    ]


def test_xg10_refund_does_not_touch_revenue() -> None:
    lines = recipes.refund(MERCHANT, Operator.GOPAY, from_pending=700, from_available=300)
    validate_lines(lines)
    assert all(line.account.kind is not AccountKind.XAALIS_REVENUE for line in lines)


@pytest.mark.parametrize(
    ("kind", "merchant", "operator"),
    [
        (AccountKind.MERCHANT_PENDING, None, None),
        (AccountKind.XAALIS_REVENUE, MERCHANT, None),
        (AccountKind.OPERATOR_CLEARING, None, None),
        (AccountKind.XAALIS_REVENUE, None, Operator.GOPAY),
    ],
)
def test_account_ref_rejects_inconsistent_qualifiers(
    kind: AccountKind, merchant: str | None, operator: Operator | None
) -> None:
    with pytest.raises(ValueError, match="exige"):
        AccountRef(kind, merchant_id=merchant, operator=operator)


# ---------------------------------------------------------------- propriétés Hypothesis


@given(st.integers(MIN_INTENT_AMOUNT, MAX_INTENT_AMOUNT), operators)
def test_xg16_property_capture_is_always_balanced(amount: int, operator: Operator) -> None:
    validate_lines(recipes.payment_capture(MERCHANT, operator, fee_breakdown(amount)))


@given(st.integers(0, 10**9), st.integers(0, 10**9), operators)
def test_xg16_property_refund_is_always_balanced(
    from_pending: int, from_available: int, operator: Operator
) -> None:
    if from_pending + from_available == 0:
        return
    validate_lines(
        recipes.refund(MERCHANT, operator, from_pending=from_pending, from_available=from_available)
    )


@given(amounts)
def test_xg16_property_settlement_and_payout_are_balanced(amount: int) -> None:
    validate_lines(recipes.settlement(MERCHANT, amount))
    validate_lines(recipes.payout(MERCHANT, amount))


@given(st.lists(st.integers(-(10**9), 10**9).filter(bool), min_size=2, max_size=8))
def test_xg16_property_unbalanced_lines_are_always_rejected(raw: list[int]) -> None:
    lines = [Line(AccountRef.revenue(), amount) for amount in raw]
    if sum(raw) == 0:
        validate_lines(lines)
    else:
        with pytest.raises(UnbalancedPostingError, match="déséquilibrée"):
            validate_lines(lines)
