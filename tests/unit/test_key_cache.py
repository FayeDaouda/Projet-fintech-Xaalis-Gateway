"""Cache court des clés authentifiées : performance sans affaiblir l'expiration des clés."""

from datetime import UTC, datetime, timedelta

import pytest

from xaalis.merchants import key_cache
from xaalis.merchants.key_cache import PrincipalCache
from xaalis.merchants.keys import KeyKind
from xaalis.merchants.service import MerchantPrincipal

DIGEST = b"d" * 32


def _principal(expires_at: datetime | None = None) -> MerchantPrincipal:
    return MerchantPrincipal("m_1", False, "key_1", KeyKind.SECRET, key_expires_at=expires_at)


def test_cached_principal_is_returned_within_ttl() -> None:
    cache = PrincipalCache(ttl_seconds=5)
    cache.put(DIGEST, _principal())
    assert cache.get(DIGEST) == _principal()


def test_entry_expires_after_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = [1_000.0]
    monkeypatch.setattr(key_cache.time, "monotonic", lambda: clock[0])
    cache = PrincipalCache(ttl_seconds=5)
    cache.put(DIGEST, _principal())
    clock[0] += 5.01
    assert cache.get(DIGEST) is None


def test_key_in_grace_period_stops_at_its_deadline_even_if_cached() -> None:
    """Rotation : l'ancienne clé ne doit pas vivre une seconde de plus grâce au cache."""
    cache = PrincipalCache(ttl_seconds=60)
    cache.put(DIGEST, _principal(expires_at=datetime.now(UTC) - timedelta(seconds=1)))
    assert cache.get(DIGEST) is None


def test_ttl_zero_disables_the_cache() -> None:
    cache = PrincipalCache(ttl_seconds=0)
    assert not cache.enabled
    cache.put(DIGEST, _principal())
    assert cache.get(DIGEST) is None


def test_memory_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(key_cache, "_MAX_ENTRIES", 3)
    cache = PrincipalCache(ttl_seconds=5)
    for i in range(4):
        cache.put(bytes([i]) * 32, _principal())
    assert cache.get(bytes([3]) * 32) is not None
    assert cache.get(bytes([0]) * 32) is None
