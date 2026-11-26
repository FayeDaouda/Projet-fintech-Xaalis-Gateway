"""Bonus gRPC (ADR 0013) : règles de scoring, en fonction pure, et fenêtre de vélocité."""

import pytest
from hypothesis import given
from hypothesis import strategies as st

from xaalis_risk.scoring import (
    DECLINE_THRESHOLD,
    MAX_SCORE,
    REVIEW_THRESHOLD,
    Decision,
    VelocityTracker,
    assess,
    decide,
    payer_fingerprint,
)

DAY = 14  # 14 h à Dakar : pas de signal « nuit »


def test_ordinary_payment_is_approved_without_reason() -> None:
    result = assess(amount=12_345, recent_requests=0, dakar_hour=DAY, blocklisted=False)
    assert (result.score_permille, result.decision, result.reasons) == (0, Decision.APPROVE, ())


def test_blocklisted_payer_is_always_declined_at_the_maximum_score() -> None:
    result = assess(amount=500, recent_requests=0, dakar_hour=DAY, blocklisted=True)
    assert (result.score_permille, result.decision) == (MAX_SCORE, Decision.DECLINE)
    assert result.reasons == ("payer_blocklisted",)


@pytest.mark.parametrize(
    ("amount", "points", "reason"),
    [
        (199_999, 0, None),
        (200_001, 80, "amount_elevated"),
        (500_001, 200, "amount_high"),
        (1_000_001, 350, "amount_very_high"),
    ],
)
def test_amount_tiers_count_once_each(amount: int, points: int, reason: str | None) -> None:
    result = assess(amount=amount, recent_requests=0, dakar_hour=DAY, blocklisted=False)
    assert result.score_permille == points
    assert result.reasons == ((reason,) if reason else ())


@pytest.mark.parametrize(
    ("recent", "points"), [(2, 0), (3, 100), (5, 250), (9, 250), (10, 450), (40, 450)]
)
def test_velocity_tiers(recent: int, points: int) -> None:
    result = assess(amount=1_000, recent_requests=recent, dakar_hour=DAY, blocklisted=False)
    assert result.score_permille == points


def test_night_round_and_large_amount_add_up_to_a_review() -> None:
    result = assess(amount=1_000_000, recent_requests=0, dakar_hour=2, blocklisted=False)
    assert result.score_permille == 350 + 100 + 50
    assert result.decision is Decision.REVIEW
    assert set(result.reasons) == {"amount_very_high", "night_time", "round_amount"}


def test_burst_of_large_payments_is_declined() -> None:
    result = assess(amount=1_250_000, recent_requests=10, dakar_hour=DAY, blocklisted=False)
    assert result.score_permille == 800
    assert result.decision is Decision.DECLINE


def test_decision_thresholds() -> None:
    assert decide(REVIEW_THRESHOLD - 1) is Decision.APPROVE
    assert decide(REVIEW_THRESHOLD) is Decision.REVIEW
    assert decide(DECLINE_THRESHOLD - 1) is Decision.REVIEW
    assert decide(DECLINE_THRESHOLD) is Decision.DECLINE


@given(
    amount=st.integers(min_value=100, max_value=2_000_000),
    recent=st.integers(min_value=0, max_value=1_000),
    hour=st.integers(min_value=0, max_value=23),
    blocklisted=st.booleans(),
)
def test_property_score_is_an_integer_between_0_and_1000_consistent_with_the_decision(
    amount: int, recent: int, hour: int, blocklisted: bool
) -> None:
    result = assess(amount=amount, recent_requests=recent, dakar_hour=hour, blocklisted=blocklisted)
    assert isinstance(result.score_permille, int)  # XG-01 : jamais de float
    assert 0 <= result.score_permille <= MAX_SCORE
    assert result.decision is decide(result.score_permille)


def test_velocity_counts_previous_requests_inside_the_window_only() -> None:
    tracker = VelocityTracker(window_seconds=600)
    payer = payer_fingerprint("+221771234567")
    assert [tracker.hit(payer, now=t) for t in (0, 10, 20)] == [0, 1, 2]
    assert tracker.hit(payer, now=605) == 2  # la demande de t=0 est sortie de la fenêtre
    assert tracker.hit(payer_fingerprint("+221761234567"), now=605) == 0  # autre payeur


def test_fingerprint_never_contains_the_phone_number() -> None:
    fingerprint = payer_fingerprint("+221771234567")
    assert "771234567" not in fingerprint
    assert len(fingerprint) == 64
