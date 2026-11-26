"""Cache court des clés d'API authentifiées, par processus (performance, cf. docs/load-test.md).

Sans cache, chaque requête ouvre une session et une connexion de plus, rien que pour
vérifier la clé. Une entrée vit ``ttl`` secondes au plus (5 par défaut) et **jamais
au-delà de l'expiration de la clé** : une clé en période de grâce (rotation) cesse d'être
acceptée à l'instant prévu. Compromis assumé : une révocation peut mettre ``ttl`` secondes
à prendre effet. ``ttl = 0`` désactive le cache. Les échecs ne sont pas mis en cache.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

from xaalis.merchants.service import MerchantPrincipal

_MAX_ENTRIES: Final = 10_000


@dataclass(frozen=True, slots=True)
class _Entry:
    principal: MerchantPrincipal
    valid_until: float  # horloge monotone


class PrincipalCache:
    def __init__(self, ttl_seconds: int) -> None:
        self._ttl = ttl_seconds
        self._entries: dict[bytes, _Entry] = {}

    @property
    def enabled(self) -> bool:
        return self._ttl > 0

    def get(self, fingerprint: bytes) -> MerchantPrincipal | None:
        entry = self._entries.get(fingerprint)
        if entry is None:
            return None
        expires_at = entry.principal.key_expires_at
        if time.monotonic() >= entry.valid_until or (
            expires_at is not None and datetime.now(UTC) >= expires_at
        ):
            self._entries.pop(fingerprint, None)
            return None
        return entry.principal

    def put(self, fingerprint: bytes, principal: MerchantPrincipal) -> None:
        if not self.enabled:
            return
        if len(self._entries) >= _MAX_ENTRIES:
            self._entries.clear()  # borne mémoire simple : le cache se reconstruit vite
        self._entries[fingerprint] = _Entry(principal, time.monotonic() + self._ttl)

    def clear(self) -> None:
        self._entries.clear()
