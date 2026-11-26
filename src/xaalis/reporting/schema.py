"""Schéma GraphQL du tableau de bord marchand (Strawberry, lecture seule).

- Toutes les données sont confinées au marchand et à l'environnement de la clé (XG-12).
- Pas de N+1 : remboursements et historique d'une liste d'intents sont chargés par
  ``DataLoader``, une requête SQL par type quel que soit le nombre d'intents.
- Montants : scalaire ``Amount`` (entier 64 bits, XOF) — le ``Int`` GraphQL plafonne à
  2³¹ − 1, que des volumes cumulés peuvent dépasser. Taux de succès en ‰ entiers (XG-01).
- Chaque résolveur ouvre sa propre session : Strawberry résout les champs en parallèle,
  et une ``AsyncSession`` ne supporte pas d'opérations concurrentes.
- Garde-fous (OWASP API4) : profondeur, alias et nombre de jetons limités.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from collections.abc import Sequence
from typing import Annotated, Any, NewType, cast

import strawberry
from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from strawberry.dataloader import DataLoader
from strawberry.extensions import MaxAliasesLimiter, MaxTokensLimiter, QueryDepthLimiter
from strawberry.fastapi import BaseContext
from strawberry.scalars import JSON
from strawberry.schema.config import StrawberryConfig
from strawberry.types import Info

from xaalis.core.clock import business_day_bounds
from xaalis.core.operators import Operator
from xaalis.core.phone import mask_phone
from xaalis.payments.models import PaymentEvent, PaymentIntent
from xaalis.payments.service import Scope
from xaalis.payments.state_machine import IntentStatus
from xaalis.refunds.models import Refund

MAX_PAGE: int = 100
MAX_RANGE_DAYS: int = 366
DAKAR_ZONE: str = "Africa/Dakar"
_SUCCEEDED = (
    IntentStatus.SUCCEEDED.value,
    IntentStatus.PARTIALLY_REFUNDED.value,
    IntentStatus.REFUNDED.value,
)
_FINISHED = (*_SUCCEEDED, IntentStatus.FAILED.value, IntentStatus.EXPIRED.value)

Amount = NewType("Amount", int)
AMOUNT_SCALAR = strawberry.scalar(
    name="Amount",
    serialize=int,
    parse_value=int,
    description="Montant entier en francs CFA (XOF), 64 bits.",
)
IntentStatusEnum = strawberry.enum(IntentStatus, name="IntentStatus")
OperatorEnum = strawberry.enum(Operator, name="Operator")


# --------------------------------------------------------------------------- contexte


class ReportingContext(BaseContext):
    def __init__(self, scope: Scope, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        super().__init__()
        self.scope = scope
        self.sessionmaker = sessionmaker
        self.refunds: DataLoader[str, list[RefundType]] = DataLoader(load_fn=self._load_refunds)
        self.history: DataLoader[str, list[StatusChange]] = DataLoader(load_fn=self._load_history)

    async def _load_refunds(self, intent_ids: list[str]) -> list[list[RefundType]]:
        async with self.sessionmaker() as session:
            rows = await session.scalars(
                select(Refund)
                .where(
                    Refund.intent_id.in_(intent_ids),
                    Refund.merchant_id == self.scope.merchant_id,
                    Refund.livemode == self.scope.livemode,
                )
                .order_by(Refund.id)
            )
            grouped: defaultdict[str, list[RefundType]] = defaultdict(list)
            for refund in rows:
                grouped[refund.intent_id].append(RefundType.of(refund))
        return [grouped[intent_id] for intent_id in intent_ids]

    async def _load_history(self, intent_ids: list[str]) -> list[list[StatusChange]]:
        async with self.sessionmaker() as session:
            rows = await session.scalars(
                select(PaymentEvent)
                .join(PaymentIntent, PaymentIntent.id == PaymentEvent.intent_id)
                .where(
                    PaymentEvent.intent_id.in_(intent_ids),
                    PaymentIntent.merchant_id == self.scope.merchant_id,
                    PaymentIntent.livemode == self.scope.livemode,
                )
                .order_by(PaymentEvent.id)
            )
            grouped: defaultdict[str, list[StatusChange]] = defaultdict(list)
            for event in rows:
                grouped[event.intent_id].append(StatusChange.of(event))
        return [grouped[intent_id] for intent_id in intent_ids]


ReportingInfo = Info[ReportingContext, None]


# --------------------------------------------------------------------------- types


@strawberry.type(name="Refund")
class RefundType:
    id: strawberry.ID
    amount: Amount
    reason: str | None
    created_at: dt.datetime

    @classmethod
    def of(cls, refund: Refund) -> RefundType:
        return cls(
            id=strawberry.ID(refund.id),
            amount=Amount(refund.amount),
            reason=refund.reason,
            created_at=refund.created_at,
        )


@strawberry.type
class StatusChange:
    from_status: str | None
    to_status: str
    cause: str
    occurred_at: dt.datetime

    @classmethod
    def of(cls, event: PaymentEvent) -> StatusChange:
        return cls(
            from_status=event.from_status,
            to_status=event.to_status,
            cause=event.cause,
            occurred_at=event.created_at,
        )


@strawberry.type(name="PaymentIntent")
class PaymentIntentType:
    id: strawberry.ID
    status: IntentStatusEnum  # type: ignore[valid-type]
    operator: OperatorEnum | None  # type: ignore[valid-type]
    amount: Amount
    fee: Amount
    net: Amount
    amount_refunded: Amount
    currency: str
    payer_phone: str = strawberry.field(description="Toujours masqué (XG-03).")
    metadata: JSON
    created_at: dt.datetime
    expires_at: dt.datetime
    succeeded_at: dt.datetime | None

    @strawberry.field(description="Remboursements (chargés par DataLoader).")
    async def refunds(self, info: ReportingInfo) -> list[RefundType]:
        return await info.context.refunds.load(str(self.id))

    @strawberry.field(description="Historique des transitions (chargé par DataLoader).")
    async def history(self, info: ReportingInfo) -> list[StatusChange]:
        return await info.context.history.load(str(self.id))

    @classmethod
    def of(cls, intent: PaymentIntent) -> PaymentIntentType:
        return cls(
            id=strawberry.ID(intent.id),
            status=IntentStatus(intent.status),
            operator=Operator(intent.operator) if intent.operator else None,
            amount=Amount(intent.amount),
            fee=Amount(intent.fee),
            net=Amount(intent.net),
            amount_refunded=Amount(intent.amount_refunded),
            currency=intent.currency,
            payer_phone=mask_phone(intent.payer_phone),
            metadata=cast(JSON, dict(intent.metadata_)),
            created_at=intent.created_at,
            expires_at=intent.expires_at,
            succeeded_at=intent.succeeded_at,
        )


@strawberry.type
class PaymentIntentConnection:
    nodes: list[PaymentIntentType]
    has_next_page: bool
    end_cursor: str | None


@strawberry.type
class DailyVolume:
    date: dt.date
    operator: OperatorEnum  # type: ignore[valid-type]
    payments: int = strawberry.field(description="Paiements confirmés ce jour-là.")
    succeeded: int
    volume: Amount = strawberry.field(description="Montant des paiements réussis.")
    fees: Amount


@strawberry.type
class OperatorSuccessRate:
    operator: OperatorEnum  # type: ignore[valid-type]
    finished: int = strawberry.field(description="Paiements arrivés à une issue finale.")
    succeeded: int
    success_rate_permille: int = strawberry.field(description="Taux de succès en ‰ (entier).")


@strawberry.type
class Revenue:
    currency: str
    gross: Amount = strawberry.field(description="Montant des paiements réussis.")
    fees: Amount = strawberry.field(description="Commissions Xaalis.")
    refunded: Amount
    net: Amount = strawberry.field(description="Revenu net du marchand.")


# --------------------------------------------------------------------------- requêtes


def _bounds(date_from: dt.date, date_to: dt.date) -> tuple[dt.datetime, dt.datetime]:
    if date_to < date_from:
        msg = "dateTo doit être postérieure ou égale à dateFrom"
        raise ValueError(msg)
    if (date_to - date_from).days >= MAX_RANGE_DAYS:
        msg = f"période limitée à {MAX_RANGE_DAYS} jours"
        raise ValueError(msg)
    return business_day_bounds(date_from)[0], business_day_bounds(date_to)[1]


def _scoped(query: Select[Any], scope: Scope) -> Select[Any]:
    return query.where(
        PaymentIntent.merchant_id == scope.merchant_id,
        PaymentIntent.livemode == scope.livemode,
    )


def _count_if(condition: Any) -> Any:
    return func.count().filter(condition)


def _sum_if(column: Any, condition: Any) -> Any:
    return func.coalesce(func.sum(column).filter(condition), 0)


@strawberry.type
class Query:
    @strawberry.field(description="Intents du marchand, filtrables, paginés par curseur.")
    async def payment_intents(
        self,
        info: ReportingInfo,
        status: IntentStatusEnum | None = None,  # type: ignore[valid-type]
        operator: OperatorEnum | None = None,  # type: ignore[valid-type]
        date_from: dt.date | None = None,
        date_to: dt.date | None = None,
        first: int = 20,
        after: str | None = None,
    ) -> PaymentIntentConnection:
        size = max(1, min(first, MAX_PAGE))
        query = _scoped(select(PaymentIntent), info.context.scope)
        if status is not None:
            query = query.where(PaymentIntent.status == IntentStatus(status).value)
        if operator is not None:
            query = query.where(PaymentIntent.operator == Operator(operator).value)
        if date_from is not None:
            query = query.where(PaymentIntent.created_at >= business_day_bounds(date_from)[0])
        if date_to is not None:
            query = query.where(PaymentIntent.created_at < business_day_bounds(date_to)[1])
        if after is not None:
            query = query.where(PaymentIntent.id < after)
        async with info.context.sessionmaker() as session:
            rows: Sequence[PaymentIntent] = (
                await session.scalars(query.order_by(PaymentIntent.id.desc()).limit(size + 1))
            ).all()
        page = [PaymentIntentType.of(row) for row in rows[:size]]
        has_next = len(rows) > size
        return PaymentIntentConnection(
            nodes=page, has_next_page=has_next, end_cursor=str(page[-1].id) if has_next else None
        )

    @strawberry.field
    async def payment_intent(
        self,
        info: ReportingInfo,
        intent_id: Annotated[strawberry.ID, strawberry.argument(name="id")],
    ) -> PaymentIntentType | None:
        query = _scoped(select(PaymentIntent), info.context.scope).where(
            PaymentIntent.id == str(intent_id)
        )
        async with info.context.sessionmaker() as session:
            intent = await session.scalar(query)
        return PaymentIntentType.of(intent) if intent else None

    @strawberry.field(description="Volumes par jour (heure de Dakar) et par opérateur.")
    async def daily_volumes(
        self, info: ReportingInfo, date_from: dt.date, date_to: dt.date
    ) -> list[DailyVolume]:
        start, end = _bounds(date_from, date_to)
        day = func.date(func.timezone(DAKAR_ZONE, PaymentIntent.created_at))
        succeeded = PaymentIntent.status.in_(_SUCCEEDED)
        query = _scoped(
            select(
                day,
                PaymentIntent.operator,
                func.count(),
                _count_if(succeeded),
                _sum_if(PaymentIntent.amount, succeeded),
                _sum_if(PaymentIntent.fee, succeeded),
            ),
            info.context.scope,
        ).where(
            PaymentIntent.created_at >= start,
            PaymentIntent.created_at < end,
            PaymentIntent.operator.is_not(None),
        )
        async with info.context.sessionmaker() as session:
            rows = await session.execute(
                query.group_by(day, PaymentIntent.operator).order_by(day, PaymentIntent.operator)
            )
        return [
            DailyVolume(
                date=d,
                operator=Operator(op),
                payments=n,
                succeeded=ok,
                volume=Amount(int(volume)),
                fees=Amount(int(fees)),
            )
            for d, op, n, ok, volume, fees in rows
        ]

    @strawberry.field(description="Taux de succès par opérateur sur la période, en ‰.")
    async def success_rates(
        self, info: ReportingInfo, date_from: dt.date, date_to: dt.date
    ) -> list[OperatorSuccessRate]:
        start, end = _bounds(date_from, date_to)
        succeeded = PaymentIntent.status.in_(_SUCCEEDED)
        query = _scoped(
            select(PaymentIntent.operator, func.count(), _count_if(succeeded)),
            info.context.scope,
        ).where(
            PaymentIntent.created_at >= start,
            PaymentIntent.created_at < end,
            PaymentIntent.status.in_(_FINISHED),
            PaymentIntent.operator.is_not(None),
        )
        async with info.context.sessionmaker() as session:
            rows = await session.execute(
                query.group_by(PaymentIntent.operator).order_by(PaymentIntent.operator)
            )
        return [
            OperatorSuccessRate(
                operator=Operator(op),
                finished=finished,
                succeeded=ok,
                success_rate_permille=ok * 1_000 // finished if finished else 0,
            )
            for op, finished, ok in rows
        ]

    @strawberry.field(description="Revenus de la période : brut, commissions, remboursés, net.")
    async def revenue(self, info: ReportingInfo, date_from: dt.date, date_to: dt.date) -> Revenue:
        start, end = _bounds(date_from, date_to)
        succeeded = PaymentIntent.status.in_(_SUCCEEDED)
        query = _scoped(
            select(
                _sum_if(PaymentIntent.amount, succeeded),
                _sum_if(PaymentIntent.fee, succeeded),
                _sum_if(PaymentIntent.amount_refunded, succeeded),
            ),
            info.context.scope,
        ).where(PaymentIntent.created_at >= start, PaymentIntent.created_at < end)
        async with info.context.sessionmaker() as session:
            gross, fees, refunded = (int(v) for v in (await session.execute(query)).one())
        return Revenue(
            currency="XOF",
            gross=Amount(gross),
            fees=Amount(fees),
            refunded=Amount(refunded),
            net=Amount(gross - fees - refunded),
        )


schema = strawberry.Schema(
    query=Query,
    config=StrawberryConfig(scalar_map={Amount: AMOUNT_SCALAR}),
    # Fabriques : une extension neuve par requête (pas d'état partagé entre requêtes).
    extensions=[
        lambda: QueryDepthLimiter(max_depth=6),
        lambda: MaxAliasesLimiter(max_alias_count=15),
        lambda: MaxTokensLimiter(max_token_count=2_000),
    ],
)
