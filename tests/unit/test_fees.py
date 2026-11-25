"""Commission Xaalis : XG-04 (1,8 % arrondi au supérieur) et XG-05 (bornes 100 / 5 000)."""

import math
from fractions import Fraction

import pytest
from hypothesis import given
from hypothesis import strategies as st

from xaalis.core.fees import FEE_MAX, FEE_MIN, compute_fee, fee_breakdown
from xaalis.core.money import MAX_INTENT_AMOUNT, MIN_INTENT_AMOUNT, InvalidAmountError

intent_amounts = st.integers(min_value=MIN_INTENT_AMOUNT, max_value=MAX_INTENT_AMOUNT)


@pytest.mark.parametrize(
    ("amount", "fee", "net"),
    [
        (1_000, 100, 900),  # 18 → minimum
        (10_000, 180, 9_820),
        (12_345, 223, 12_122),  # 222,21 → ceil
        (500_000, 5_000, 495_000),  # 9 000 → maximum
    ],
)
def test_xg04_xg05_examples_from_the_specification(amount: int, fee: int, net: int) -> None:
    breakdown = fee_breakdown(amount)
    assert (breakdown.fee, breakdown.net) == (fee, net)


def test_xg04_fee_is_rounded_up_never_down() -> None:
    assert compute_fee(12_345) == 223
    assert compute_fee(55_556) == 1_001  # 1 000,008 → 1 001


def test_xg04_exact_percentage_is_not_rounded() -> None:
    assert compute_fee(50_000) == 900


def test_xg05_minimum_fee_applies_below_threshold() -> None:
    # 1,8 % < 100 tant que le montant ≤ 5 555 XOF.
    assert compute_fee(5_555) == FEE_MIN
    assert compute_fee(5_556) == 101


def test_xg05_maximum_fee_applies_above_threshold() -> None:
    assert compute_fee(277_777) == 5_000
    assert compute_fee(2_000_000) == FEE_MAX


def test_xg05_minimum_fee_can_absorb_the_whole_amount() -> None:
    breakdown = fee_breakdown(MIN_INTENT_AMOUNT)
    assert breakdown.fee == breakdown.amount == 100
    assert breakdown.net == 0


@pytest.mark.parametrize("amount", [99, 2_000_001, 0, -1])
def test_xg01_fee_rejects_amounts_outside_intent_bounds(amount: int) -> None:
    with pytest.raises(InvalidAmountError):
        compute_fee(amount)


@given(intent_amounts)
def test_xg05_property_fee_always_within_bounds(amount: int) -> None:
    fee = compute_fee(amount)
    assert isinstance(fee, int)
    assert FEE_MIN <= fee <= FEE_MAX


@given(intent_amounts)
def test_xg04_property_fee_matches_reference_formula(amount: int) -> None:
    # Référence indépendante, en fraction exacte (aucun flottant, même dans les tests).
    expected = min(max(math.ceil(Fraction(amount * 18, 1000)), FEE_MIN), FEE_MAX)
    assert compute_fee(amount) == expected


@given(intent_amounts)
def test_xg04_property_net_plus_fee_equals_amount(amount: int) -> None:
    breakdown = fee_breakdown(amount)
    assert breakdown.net + breakdown.fee == amount
    assert breakdown.net >= 0
