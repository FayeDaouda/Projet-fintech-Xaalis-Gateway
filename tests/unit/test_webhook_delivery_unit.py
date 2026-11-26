"""Relances : 8 tentatives sur ≈ 24 h, jitter borné ; secrets chiffrés au repos."""

import random

from xaalis.config import Settings
from xaalis.webhooks import crypto
from xaalis.webhooks.delivery import next_delay_seconds

SCHEDULE = Settings().webhook_retry_schedule_seconds


def test_eight_attempts_spread_over_about_twenty_four_hours() -> None:
    assert len(SCHEDULE) + 1 == 8
    assert 22 * 3600 <= sum(SCHEDULE) <= 25 * 3600


def test_delays_grow_and_jitter_stays_within_twenty_percent() -> None:
    rng = random.Random(1)
    for attempt, base in enumerate(SCHEDULE, start=1):
        for _ in range(50):
            delay = next_delay_seconds(SCHEDULE, attempt, rng)
            assert delay is not None
            assert base * 8 // 10 <= delay <= base * 12 // 10
    assert sorted(SCHEDULE) == SCHEDULE


def test_no_retry_after_the_eighth_attempt() -> None:
    assert next_delay_seconds(SCHEDULE, 8, random.Random()) is None


def test_endpoint_secret_is_encrypted_at_rest() -> None:
    secret = crypto.generate_secret()
    ciphertext = crypto.encrypt("key-material-" + "k" * 30, secret)
    assert secret.encode() not in ciphertext
    assert crypto.decrypt("key-material-" + "k" * 30, ciphertext) == secret
    assert secret.startswith("whsec_")
