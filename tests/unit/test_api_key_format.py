"""Clés d'API : format, entropie, empreinte (ADR 0004)."""

import pytest

from xaalis.merchants.keys import KeyKind, fingerprint, generate_key, parse_key, redact

PEPPER = "unit-test-pepper-" + "p" * 32


@pytest.mark.parametrize(
    ("kind", "livemode", "prefix"),
    [
        (KeyKind.SECRET, False, "sk_test_"),
        (KeyKind.SECRET, True, "sk_live_"),
        (KeyKind.PUBLISHABLE, False, "pk_test_"),
        (KeyKind.PUBLISHABLE, True, "pk_live_"),
    ],
)
def test_generated_key_has_expected_prefix_and_parses_back(
    kind: KeyKind, livemode: bool, prefix: str
) -> None:
    key = generate_key(kind, livemode=livemode)
    assert key.startswith(prefix)
    assert len(key) == len(prefix) + 32
    parsed = parse_key(key)
    assert parsed is not None
    assert (parsed.kind, parsed.livemode) == (kind, livemode)


def test_generated_keys_are_unique() -> None:
    keys = {generate_key(KeyKind.SECRET, livemode=False) for _ in range(1_000)}
    assert len(keys) == 1_000


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "sk_test_",
        "sk_prod_" + "a" * 32,
        "xx_test_" + "a" * 32,
        "sk_test_" + "a" * 31,
        "sk_test_" + "-" * 32,
        "Bearer sk_test_" + "a" * 32,
    ],
)
def test_malformed_keys_are_rejected_without_lookup(raw: str) -> None:
    assert parse_key(raw) is None


def test_fingerprint_depends_on_pepper_and_is_32_bytes() -> None:
    key = generate_key(KeyKind.SECRET, livemode=False)
    digest = fingerprint(PEPPER, key)
    assert len(digest) == 32
    assert digest == fingerprint(PEPPER, key)
    assert digest != fingerprint(PEPPER + "x", key)


def test_redacted_form_reveals_only_the_last_four_characters() -> None:
    key = generate_key(KeyKind.SECRET, livemode=True)
    shown = redact(key)
    assert shown == f"sk_live_…{key[-4:]}"
    assert key[8:-4] not in shown
