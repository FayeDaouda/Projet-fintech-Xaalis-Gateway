"""Client HTTP des opérateurs : timeouts, reprises, et classement prudent des réponses.

La question centrale n'est pas « l'appel a-t-il échoué ? » mais « le payeur a-t-il pu
être débité ? ». Chaque réponse est classée en conséquence :

==============  ===========================================  ===============================
Résultat        Cas                                          Bascule vers un autre opérateur
==============  ===========================================  ===============================
ACCEPTED        2xx                                          — (on attend le callback)
UNAVAILABLE     connexion impossible, 503, 502, 429          oui : rien n'a été traité
REJECTED        4xx (requête refusée sur le fond)            oui : rien n'a été traité
AMBIGUOUS       timeout de lecture, 500, 504, coupure        **jamais** : intent épinglé
==============  ===========================================  ===============================

Chaque demande porte la référence de l'intent et l'opérateur la déduplique : une reprise
auprès du MÊME opérateur ne débite jamais deux fois. Après une réponse ambiguë, on ne
réessaie donc qu'auprès de lui (ADR 0007).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final

import httpx

from xaalis.core.operators import Operator
from xaalis.observability.logging import get_logger

_log = get_logger(__name__)
_SAFE_TO_RETRY_STATUSES: Final = frozenset({429, 502, 503})


class ChargeStatus(StrEnum):
    ACCEPTED = "accepted"
    UNAVAILABLE = "unavailable"
    REJECTED = "rejected"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True, slots=True)
class ChargeResult:
    status: ChargeStatus
    operator_reference: str | None = None
    detail: str = ""


class OperatorClient:
    def __init__(self, http: httpx.AsyncClient, *, callback_base_url: str) -> None:
        self._http = http
        self._callback_base_url = callback_base_url.rstrip("/")

    def callback_url(self, operator: Operator) -> str:
        return f"{self._callback_base_url}/v1/operators/{operator.value}/callback"

    async def charge(
        self,
        operator: Operator,
        *,
        intent_id: str,
        amount: int,
        payer_phone: str,
        request_id: str | None,
    ) -> ChargeResult:
        body = {
            "reference": intent_id,
            "amount": amount,
            "currency": "XOF",
            "payer_phone": payer_phone,
            "callback_url": self.callback_url(operator),
        }
        headers = {"X-Request-Id": request_id} if request_id else {}
        try:
            response = await self._http.post(
                f"/{operator.value}/charges", json=body, headers=headers
            )
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
            # La requête n'a jamais atteint l'opérateur.
            return ChargeResult(ChargeStatus.UNAVAILABLE, detail=type(exc).__name__)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            # Envoyée, mais réponse perdue : l'opérateur a PEUT-ÊTRE débité.
            return ChargeResult(ChargeStatus.AMBIGUOUS, detail=type(exc).__name__)
        return self._classify(response)

    @staticmethod
    def _classify(response: httpx.Response) -> ChargeResult:
        code = response.status_code
        if 200 <= code < 300:
            reference = response.json().get("operator_reference")
            if isinstance(reference, str) and reference:
                return ChargeResult(ChargeStatus.ACCEPTED, operator_reference=reference)
            return ChargeResult(ChargeStatus.AMBIGUOUS, detail="réponse 2xx sans référence")
        if code in _SAFE_TO_RETRY_STATUSES:
            return ChargeResult(ChargeStatus.UNAVAILABLE, detail=f"HTTP {code}")
        if 400 <= code < 500:
            return ChargeResult(ChargeStatus.REJECTED, detail=f"HTTP {code}")
        return ChargeResult(ChargeStatus.AMBIGUOUS, detail=f"HTTP {code}")


def build_http_client(base_url: str, *, connect_ms: int, read_ms: int) -> httpx.AsyncClient:
    timeout = httpx.Timeout(read_ms / 1000, connect=connect_ms / 1000)
    return httpx.AsyncClient(base_url=base_url, timeout=timeout)
