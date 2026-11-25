"""Signature HMAC horodatée des requêtes sensibles (lot 1, tolérance 300 s)."""

import pytest

from xaalis.core.signatures import SignatureFailure, sign, verify

SECRET = "sk_test_" + "s" * 32
NOW = 1_795_000_000
BODY = b'{"payment_intent":"pi_1","amount":1000}'


def _verify(
    timestamp: str | None, signature: str | None, body: bytes = BODY
) -> SignatureFailure | None:
    return verify(SECRET, timestamp=timestamp, signature=signature, body=body, now=NOW)


def test_valid_signature_is_accepted() -> None:
    assert _verify(str(NOW), sign(SECRET, NOW, BODY)) is None


def test_v1_prefixed_signature_is_accepted() -> None:
    assert _verify(str(NOW), "v1=" + sign(SECRET, NOW, BODY)) is None


@pytest.mark.parametrize("offset", [-300, 300])
def test_timestamp_at_tolerance_boundary_is_accepted(offset: int) -> None:
    ts = NOW + offset
    assert _verify(str(ts), sign(SECRET, ts, BODY)) is None


@pytest.mark.parametrize("offset", [-301, 301])
def test_timestamp_outside_tolerance_is_rejected(offset: int) -> None:
    ts = NOW + offset
    assert _verify(str(ts), sign(SECRET, ts, BODY)) is SignatureFailure.STALE


def test_tampered_body_is_rejected() -> None:
    signature = sign(SECRET, NOW, BODY)
    tampered = BODY.replace(b"1000", b"9000")
    assert _verify(str(NOW), signature, tampered) is SignatureFailure.MISMATCH


def test_signature_with_another_secret_is_rejected() -> None:
    other = sign("sk_test_" + "o" * 32, NOW, BODY)
    assert _verify(str(NOW), other) is SignatureFailure.MISMATCH


@pytest.mark.parametrize(
    ("timestamp", "signature"),
    [(None, "abc"), (str(NOW), None), ("", "abc"), ("12a", "abc"), ("9" * 13, "abc")],
)
def test_missing_or_malformed_headers_are_rejected(
    timestamp: str | None, signature: str | None
) -> None:
    assert _verify(timestamp, signature) is SignatureFailure.MALFORMED
