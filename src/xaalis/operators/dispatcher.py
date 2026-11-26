"""Relais des demandes de débit : outbox → opérateur, avec disjoncteur et bascule (lot 3).

Pour un intent ``processing``, on essaie les opérateurs dans l'ordre du plan de routage
(l'opérateur courant d'abord). Règle d'or : **on ne bascule que si l'on sait que le
précédent n'a rien débité** (voir ``client.ChargeStatus`` et ADR 0007) :

- disjoncteur ouvert (rien envoyé), 503, connexion refusée, 4xx → opérateur suivant ;
- réponse ambiguë (timeout, 500…) → l'intent est **épinglé** à cet opérateur : les reprises
  ne visent que lui, avec la même référence, qu'il déduplique. Jamais de second débit.

Avant le premier envoi, le paiement passe une fois par la barrière anti-fraude (gRPC,
deadline 150 ms, ADR 0013) : refus → ``failed`` sans débit ; service indisponible →
repli (petits montants acceptés, gros montants mis en attente).
"""

from __future__ import annotations

import asyncio
from typing import Final

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from xaalis.core.operators import Operator
from xaalis.observability.logging import get_logger
from xaalis.observability.tracing import continue_trace
from xaalis.operators.client import ChargeResult, ChargeStatus, OperatorClient
from xaalis.outbox import service as outbox
from xaalis.outbox.models import OutboxMessage
from xaalis.payments import service as payments
from xaalis.payments.models import PaymentIntent
from xaalis.payments.state_machine import IntentStatus
from xaalis.risk.gate import RiskQuery, RiskScorer
from xaalis.routing.health import OperatorHealth

_log = get_logger(__name__)
_ACTOR: Final = "system:dispatcher"
MAX_BACKOFF_SECONDS: Final = 30
PINNED_KEY: Final = "pinned_operator"


def backoff_seconds(attempts: int) -> int:
    # 1, 2, 4, 8, 16, puis 30 s : décalage binaire, l'exposant reste borné.
    return min(1 << min(max(attempts - 1, 0), 5), MAX_BACKOFF_SECONDS)


class Dispatcher:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        client: OperatorClient,
        health: OperatorHealth,
        risk: RiskScorer | None = None,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._client = client
        self._health = health
        self._risk = risk  # None : pas de barrière (tests du routage seul)

    async def run_batch(self, *, limit: int = 20) -> int:
        async with self._sessionmaker.begin() as session:
            messages = await outbox.claim(
                session, topics=[payments.OPERATOR_CHARGE_TOPIC], limit=limit
            )
        await asyncio.gather(*(self._safely(message) for message in messages))
        return len(messages)

    async def _safely(self, message: OutboxMessage) -> None:
        try:
            await self.dispatch(message)
        except Exception as exc:  # noqa: BLE001 — un message en échec ne bloque pas les autres
            _log.exception("dispatch_crashed", outbox_id=message.id)
            await self._retry(message, f"crash: {type(exc).__name__}")

    async def dispatch(self, message: OutboxMessage) -> None:
        with continue_trace(
            "operator charge",
            message.traceparent,
            {"xaalis.intent_id": message.aggregate_id, "xaalis.attempt": message.attempts},
        ):
            await self._dispatch(message)

    async def _dispatch(self, message: OutboxMessage) -> None:
        structlog.contextvars.bind_contextvars(
            request_id=message.request_id, intent_id=message.aggregate_id
        )
        async with self._sessionmaker() as session:
            intent = await session.get(PaymentIntent, message.aggregate_id)
        if intent is None or intent.status != IntentStatus.PROCESSING.value:
            await self._complete(message)
            return
        needs_screening = self._risk is not None and intent.risk_decision is None
        if needs_screening and not await self._screen(message, intent):
            return

        for operator in self._attempt_order(intent, message):
            if not await self._health.allow(operator):
                _log.info("operator_skipped_breaker_open", operator=operator.value)
                continue
            async with self._sessionmaker.begin() as session:
                selected = await payments.select_operator(
                    session, intent.id, operator, actor=_ACTOR
                )
            if selected is None:  # expiré ou annulé entre-temps : plus rien à envoyer
                await self._complete(message)
                return

            result = await self._client.charge(
                operator,
                intent_id=intent.id,
                amount=intent.amount,
                payer_phone=intent.payer_phone,
                request_id=message.request_id,
            )
            if result.status is ChargeStatus.AMBIGUOUS:
                # L'opérateur a PEUT-ÊTRE débité : interdiction de basculer. On épingle.
                await self._health.record_failure(operator)
                _log.warning("charge_outcome_ambiguous", operator=operator.value)
                await self._retry(
                    message,
                    f"ambiguous {operator.value}: {result.detail}",
                    pin=operator,
                )
                return

            if result.status is ChargeStatus.ACCEPTED:
                await self._accepted(message, intent, operator, result)
                return
            if result.status is ChargeStatus.UNAVAILABLE:
                await self._health.record_failure(operator)
            _log.warning("operator_failover", operator=operator.value, reason=result.detail)

        await self._retry(message, "aucun opérateur disponible")

    async def _screen(self, message: OutboxMessage, intent: PaymentIntent) -> bool:
        """Barrière anti-fraude, hors transaction (aucun appel réseau sous verrou).

        Renvoie ``True`` si le débit peut partir, ``False`` si le message a été traité
        (refus définitif) ou replanifié (attente du service de scoring).
        """
        assert self._risk is not None  # noqa: S101 — garanti par l'appelant
        verdict = await self._risk.score(
            RiskQuery(
                intent_id=intent.id,
                merchant_id=intent.merchant_id,
                livemode=intent.livemode,
                amount=intent.amount,
                payer_phone=intent.payer_phone,
            )
        )
        if verdict.decision is None:  # repli « attente » : rien n'est envoyé
            await self._retry(message, "risque : service de scoring indisponible")
            return False
        async with self._sessionmaker.begin() as session:
            assessed = await payments.apply_risk_verdict(
                session,
                intent.id,
                decision=verdict.decision.value,
                source=verdict.source.value,
                score_permille=verdict.score_permille,
                reasons=verdict.reasons,
                actor="system:risk",
            )
        if assessed.status != IntentStatus.PROCESSING.value:
            _log.info("charge_skipped_after_risk", status=assessed.status)
            await self._complete(message)
            return False
        return True

    @staticmethod
    def _attempt_order(intent: PaymentIntent, message: OutboxMessage) -> list[Operator]:
        pinned = message.payload.get(PINNED_KEY)
        if pinned:
            return [Operator(pinned)]  # après une ambiguïté : cet opérateur et lui seul
        candidates = [Operator(value) for value in message.payload.get("candidates", [])]
        current = Operator(intent.operator) if intent.operator else None
        ordered = ([current] if current else []) + [op for op in candidates if op != current]
        return ordered

    async def _accepted(
        self,
        message: OutboxMessage,
        intent: PaymentIntent,
        operator: Operator,
        result: ChargeResult,
    ) -> None:
        await self._health.record_success(operator)
        async with self._sessionmaker.begin() as session:
            if result.operator_reference:
                await payments.record_operator_acceptance(
                    session, intent.id, operator, result.operator_reference
                )
            await outbox.complete(session, message.id)
        _log.info("charge_accepted", operator=operator.value)

    async def _complete(self, message: OutboxMessage) -> None:
        async with self._sessionmaker.begin() as session:
            await outbox.complete(session, message.id)

    async def _retry(
        self, message: OutboxMessage, error: str, *, pin: Operator | None = None
    ) -> None:
        async with self._sessionmaker.begin() as session:
            await outbox.retry_later(
                session,
                message.id,
                delay_seconds=backoff_seconds(message.attempts),
                error=error,
                payload_patch={PINNED_KEY: pin.value} if pin else None,
            )
