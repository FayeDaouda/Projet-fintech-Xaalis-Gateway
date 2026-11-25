"""Contrat callbacks : ce que publie chaque opérateur (simulateur) est compris par la
passerelle, et toute altération ou mauvaise signature est rejetée."""

import hashlib
import hmac
import json
import time

import pytest

from xaalis.core.operators import Operator
from xaalis.operators.callbacks import (
    CallbackOutcome,
    InvalidCallbackSignatureError,
    MalformedCallbackError,
    parse_callback,
)
from xaalis_sim.operators import formats

SECRET = "contract-test-secret-" + "s" * 24


def _headers(request: formats.CallbackRequest) -> dict[str, str]:
    return {k.lower(): v for k, v in request.headers.items()}


@pytest.mark.parametrize("operator", list(Operator))
@pytest.mark.parametrize("ok", [True, False], ids=["success", "failure"])
def test_every_operator_format_round_trips(operator: Operator, ok: bool) -> None:
    request = formats.BUILDERS[operator.value](
        SECRET, reference="pi_ABC", operator_reference="OP-123", amount=12_345, ok=ok
    )
    callback = parse_callback(operator, _headers(request), request.body, SECRET)
    assert callback.operator is operator
    assert callback.intent_id == "pi_ABC"
    assert callback.operator_reference == "OP-123"
    assert callback.amount == 12_345
    assert callback.outcome is (CallbackOutcome.SUCCEEDED if ok else CallbackOutcome.FAILED)


def test_teranga_callback_is_form_encoded_and_signature_is_not_kept() -> None:
    request = formats.teranga(
        SECRET, reference="pi_X", operator_reference="TM-1", amount=500, ok=True
    )
    assert request.headers["Content-Type"] == "application/x-www-form-urlencoded"
    callback = parse_callback(Operator.TERANGA, _headers(request), request.body, SECRET)
    assert "signature" not in callback.payload


@pytest.mark.parametrize("operator", list(Operator))
def test_wrong_secret_is_rejected(operator: Operator) -> None:
    request = formats.BUILDERS[operator.value](
        "another-secret-" + "x" * 30, reference="pi_A", operator_reference="R", amount=100, ok=True
    )
    with pytest.raises(InvalidCallbackSignatureError):
        parse_callback(operator, _headers(request), request.body, SECRET)


@pytest.mark.parametrize("operator", list(Operator))
def test_tampered_amount_is_rejected(operator: Operator) -> None:
    request = formats.BUILDERS[operator.value](
        SECRET, reference="pi_A", operator_reference="R", amount=1_000, ok=True
    )
    tampered = request.body.replace(b"1000", b"9000")
    with pytest.raises(InvalidCallbackSignatureError):
        parse_callback(operator, _headers(request), tampered, SECRET)


def test_missing_signature_header_is_rejected() -> None:
    request = formats.gopay(SECRET, reference="pi_A", operator_reference="R", amount=100, ok=True)
    with pytest.raises(InvalidCallbackSignatureError):
        parse_callback(Operator.GOPAY, {}, request.body, SECRET)


def test_ndiaga_replayed_old_callback_is_rejected() -> None:
    body = json.dumps({"id": "NC-1", "order": "pi_A", "amount": 100, "state": "accepted"}).encode()
    old = int(time.time()) - 3_600
    sig = hmac.new(SECRET.encode(), f"{old}.".encode() + body, hashlib.sha256).hexdigest()
    with pytest.raises(InvalidCallbackSignatureError, match="ancien"):
        parse_callback(Operator.NDIAGA, {"x-ndiaga-signature": f"t={old},v1={sig}"}, body, SECRET)


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b"[1, 2]",
        b'{"transaction_id": "R", "amount": 100}',
        b'{"transaction_id": "R", "merchant_reference": "pi", "amount": "x", "status": "OK"}',
        b'{"transaction_id": "R", "merchant_reference": "pi", "amount": true, "status": "OK"}',
    ],
    ids=["invalid-json", "not-object", "missing-field", "bad-amount", "bool-amount"],
)
def test_malformed_but_correctly_signed_body_is_rejected(body: bytes) -> None:
    signature = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    with pytest.raises(MalformedCallbackError):
        parse_callback(Operator.GOPAY, {"x-gopay-signature": signature}, body, SECRET)
