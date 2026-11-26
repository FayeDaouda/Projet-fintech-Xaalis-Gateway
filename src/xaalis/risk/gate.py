"""Barrière anti-fraude du worker : appel gRPC borné à 150 ms, puis politique de repli.

Appelée une fois par paiement, juste avant le premier envoi à l'opérateur (ADR 0013) :

- le service répond → sa décision s'applique (``decline`` fait échouer le paiement avant
  tout débit ; ``review`` laisse passer en le signalant) ;
- le service est indisponible ou dépasse la deadline → **repli** :
  * montant ≤ plafond (100 000 XOF par défaut) : on laisse passer, décision tracée
    ``approve`` / source ``fallback`` — la perte possible est bornée par le plafond, et le
    payeur valide de toute façon le débit sur son téléphone ;
  * au-delà : le paiement **attend** (nouvelle tentative du worker). Si la panne dure,
    l'expiration à 15 min (XG-07) le clôt sans qu'aucun débit n'ait été demandé.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, Protocol

import grpc

from xaalis.observability.logging import get_logger
from xaalis.observability.metrics import RISK_ASSESSMENTS, RISK_LATENCY
from xaalis.risk.v1 import risk_pb2, risk_pb2_grpc

_log = get_logger(__name__)
DEFAULT_DEADLINE_MS: Final = 150


class RiskDecision(StrEnum):
    APPROVE = "approve"
    REVIEW = "review"
    DECLINE = "decline"


class RiskSource(StrEnum):
    SERVICE = "service"
    FALLBACK = "fallback"


_FROM_PROTO: Final = {
    risk_pb2.DECISION_APPROVE: RiskDecision.APPROVE,
    risk_pb2.DECISION_REVIEW: RiskDecision.REVIEW,
    risk_pb2.DECISION_DECLINE: RiskDecision.DECLINE,
}


@dataclass(frozen=True, slots=True)
class RiskVerdict:
    """Décision finale ; ``decision is None`` : « réessayer plus tard, rien n'est envoyé »."""

    decision: RiskDecision | None
    source: RiskSource
    score_permille: int | None
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RiskQuery:
    intent_id: str
    merchant_id: str
    livemode: bool
    amount: int
    payer_phone: str


class RiskScorer(Protocol):
    async def score(self, query: RiskQuery) -> RiskVerdict: ...


class RiskGate:
    def __init__(
        self,
        channel: grpc.aio.Channel,
        *,
        deadline_ms: int = DEFAULT_DEADLINE_MS,
        fail_open_max_amount: int = 100_000,
    ) -> None:
        self._stub = risk_pb2_grpc.RiskScoringStub(channel)
        self._deadline = deadline_ms / 1_000
        self._fail_open_max_amount = fail_open_max_amount

    async def score(self, query: RiskQuery) -> RiskVerdict:
        started = time.perf_counter()
        try:
            response = await self._stub.Score(
                risk_pb2.ScoreRequest(
                    payment_intent_id=query.intent_id,
                    merchant_id=query.merchant_id,
                    livemode=query.livemode,
                    amount=query.amount,
                    currency="XOF",
                    payer_phone=query.payer_phone,
                ),
                timeout=self._deadline,
            )
        except grpc.aio.AioRpcError as exc:
            RISK_LATENCY.observe(time.perf_counter() - started)
            return self._fallback(query, exc.code())
        RISK_LATENCY.observe(time.perf_counter() - started)
        decision = _FROM_PROTO.get(response.decision)
        if decision is None:  # réponse hors contrat : traitée comme une indisponibilité
            return self._fallback(query, grpc.StatusCode.INTERNAL)
        RISK_ASSESSMENTS.labels(decision=decision.value, source=RiskSource.SERVICE.value).inc()
        return RiskVerdict(
            decision, RiskSource.SERVICE, response.score_permille, tuple(response.reasons)
        )

    def _fallback(self, query: RiskQuery, code: grpc.StatusCode) -> RiskVerdict:
        reason = f"risk_unavailable_{code.name.lower()}"
        if query.amount <= self._fail_open_max_amount:
            verdict = RiskVerdict(RiskDecision.APPROVE, RiskSource.FALLBACK, None, (reason,))
            label = "approve"
        else:
            verdict = RiskVerdict(None, RiskSource.FALLBACK, None, (reason, "held_above_ceiling"))
            label = "hold"
        RISK_ASSESSMENTS.labels(decision=label, source=RiskSource.FALLBACK.value).inc()
        _log.warning("risk_fallback", code=code.name, amount=query.amount, outcome=label)
        return verdict


def open_channel(target: str) -> grpc.aio.Channel:
    return grpc.aio.insecure_channel(target)
