"""Montants : entiers XOF uniquement, bornes d'un intent (XG-01)."""

import pytest

from xaalis.core.money import InvalidAmountError, ensure_integer_amount, ensure_intent_amount


@pytest.mark.parametrize("amount", [100, 2_000_000, 15_000])
def test_xg01_amount_within_bounds_is_accepted(amount: int) -> None:
    assert ensure_intent_amount(amount) == amount


@pytest.mark.parametrize("amount", [99, 2_000_001, 0, -500])
def test_xg01_amount_outside_bounds_is_rejected(amount: int) -> None:
    with pytest.raises(InvalidAmountError, match="entre 100 et 2000000"):
        ensure_intent_amount(amount)


@pytest.mark.parametrize("amount", [1000.0, "1000", None, True])
def test_xg01_non_integer_amount_is_rejected(amount: object) -> None:
    with pytest.raises(InvalidAmountError, match="entier"):
        ensure_integer_amount(amount)
