from xaalis.idempotency.service import request_fingerprint


def test_semantically_identical_json_bodies_share_a_fingerprint() -> None:
    a = request_fingerprint("POST", "/v1/payment-intents", b'{"amount": 1000, "currency": "XOF"}')
    b = request_fingerprint("POST", "/v1/payment-intents", b'{"currency":"XOF","amount":1000}')
    assert a == b


def test_different_bodies_paths_or_methods_differ() -> None:
    base = request_fingerprint("POST", "/v1/payment-intents", b'{"amount":1000}')
    assert base != request_fingerprint("POST", "/v1/payment-intents", b'{"amount":1001}')
    assert base != request_fingerprint("POST", "/v1/refunds", b'{"amount":1000}')
    assert base != request_fingerprint("PUT", "/v1/payment-intents", b'{"amount":1000}')


def test_non_json_body_is_fingerprinted_raw() -> None:
    assert request_fingerprint("POST", "/x", b"not json") != request_fingerprint("POST", "/x", b"")
