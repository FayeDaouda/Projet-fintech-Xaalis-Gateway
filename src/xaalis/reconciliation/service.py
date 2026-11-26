"""Exécutions de rapprochement : demande, traitement par le worker, rapport.

Une demande crée une exécution ``pending`` par opérateur (source : simulateur, ou CSV
fourni). Le worker la réserve (``SKIP LOCKED``), récupère le relevé si besoin, lit le
grand livre du jour comptable (heure de Dakar, XG-15), compare, et enregistre chaque ligne.
La dernière exécution terminée d'un couple (date, opérateur) fait foi.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import date

import polars as pl
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from xaalis.core.clock import business_day_bounds
from xaalis.core.errors import NotFoundError
from xaalis.core.ids import new_id
from xaalis.core.operators import Operator
from xaalis.ledger.lines import TransactionKind
from xaalis.ledger.models import LedgerTransaction
from xaalis.observability.logging import get_logger
from xaalis.payments.models import PaymentIntent
from xaalis.reconciliation.engine import (
    InvalidStatementError,
    Outcome,
    ledger_frame,
    parse_statement,
    reconcile,
)
from xaalis.reconciliation.models import ReconciliationLine, ReconciliationRun

_log = get_logger(__name__)

StatementFetcher = Callable[[Operator, date], Awaitable[bytes]]


async def request_runs(
    session: AsyncSession,
    statement_date: date,
    *,
    operators: Sequence[Operator],
    requested_by: str,
    statement_csv: bytes | None = None,
) -> list[ReconciliationRun]:
    runs = [
        ReconciliationRun(
            id=new_id("rec"),
            statement_date=statement_date,
            operator=operator.value,
            source="upload" if statement_csv is not None else "simulator",
            status="pending",
            statement_csv=statement_csv,
            requested_by=requested_by,
        )
        for operator in operators
    ]
    session.add_all(runs)
    await session.flush()
    return runs


async def ledger_rows(
    session: AsyncSession, statement_date: date, operator: Operator
) -> list[dict[str, object]]:
    """Paiements comptabilisés (capture) le jour comptable donné, pour cet opérateur."""
    start, end = business_day_bounds(statement_date)
    rows = await session.execute(
        select(
            PaymentIntent.operator_reference,
            PaymentIntent.id,
            PaymentIntent.livemode,
            PaymentIntent.amount,
        )
        .join(LedgerTransaction, LedgerTransaction.reference == PaymentIntent.id)
        .where(
            LedgerTransaction.kind == TransactionKind.PAYMENT_CAPTURE.value,
            LedgerTransaction.created_at >= start,
            LedgerTransaction.created_at < end,
            PaymentIntent.operator == operator.value,
        )
    )
    return [
        {"operator_reference": ref, "intent_id": iid, "livemode": live, "ledger_amount": amount}
        for ref, iid, live, amount in rows
    ]


async def process_next(
    sessionmaker: async_sessionmaker[AsyncSession], fetch: StatementFetcher
) -> str | None:
    """Traite une exécution en attente ; renvoie son identifiant (ou ``None``)."""
    async with sessionmaker.begin() as session:
        run = await session.scalar(
            select(ReconciliationRun)
            .where(ReconciliationRun.status == "pending")
            .order_by(ReconciliationRun.created_at)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if run is None:
            return None
        run.status = "running"
        run_id, operator, day, csv = (
            run.id,
            Operator(run.operator),
            run.statement_date,
            run.statement_csv,
        )

    try:
        raw = csv if csv is not None else await fetch(operator, day)
        statement = parse_statement(raw, operator=operator.value)
        async with sessionmaker() as session:
            ledger = ledger_frame(await ledger_rows(session, day, operator))
        result = reconcile(statement, ledger)
    except Exception as exc:  # noqa: BLE001 — l'échec est enregistré sur l'exécution
        detail = exc.detail if isinstance(exc, InvalidStatementError) else type(exc).__name__
        _log.warning("reconciliation_failed", run_id=run_id, error=detail)
        async with sessionmaker.begin() as session:
            await session.execute(
                update(ReconciliationRun)
                .where(ReconciliationRun.id == run_id)
                .values(status="failed", error=detail[:500], completed_at=func.now())
            )
        return run_id

    async with sessionmaker.begin() as session:
        lines = result.to_dicts()
        if lines:
            session.add_all([ReconciliationLine(run_id=run_id, **line) for line in lines])
        await session.execute(
            update(ReconciliationRun)
            .where(ReconciliationRun.id == run_id)
            .values(
                status="completed",
                statement_lines=statement.height,
                matched=result.filter(pl.col("outcome") == Outcome.MATCHED.value).height,
                completed_at=func.now(),
            )
        )
    _log.info("reconciliation_completed", run_id=run_id, operator=operator.value, day=str(day))
    return run_id


async def latest_runs(session: AsyncSession, statement_date: date) -> list[ReconciliationRun]:
    """La plus récente exécution de chaque opérateur pour cette date."""
    rows = await session.scalars(
        select(ReconciliationRun)
        .where(ReconciliationRun.statement_date == statement_date)
        .ext(distinct_on(ReconciliationRun.operator))
        .order_by(ReconciliationRun.operator, ReconciliationRun.created_at.desc())
    )
    return list(rows)


@dataclass(frozen=True, slots=True)
class Report:
    statement_date: date
    runs: list[ReconciliationRun]
    lines: list[ReconciliationLine]

    @property
    def discrepancies(self) -> list[ReconciliationLine]:
        return [line for line in self.lines if line.outcome != Outcome.MATCHED.value]


async def report(session: AsyncSession, statement_date: date) -> Report:
    runs = await latest_runs(session, statement_date)
    if not runs:
        raise NotFoundError("aucun rapprochement pour cette date")
    lines = (
        await session.scalars(
            select(ReconciliationLine)
            .where(ReconciliationLine.run_id.in_([run.id for run in runs]))
            .order_by(ReconciliationLine.outcome, ReconciliationLine.operator_reference)
        )
    ).all()
    return Report(statement_date=statement_date, runs=runs, lines=list(lines))


async def matched_intents(session: AsyncSession, statement_date: date) -> dict[str, str]:
    """Intents rapprochés SANS écart par la dernière exécution terminée : intent → exécution."""
    runs = [run for run in await latest_runs(session, statement_date) if run.status == "completed"]
    if not runs:
        return {}
    rows = await session.execute(
        select(ReconciliationLine.intent_id, ReconciliationLine.run_id).where(
            ReconciliationLine.run_id.in_([run.id for run in runs]),
            ReconciliationLine.outcome == Outcome.MATCHED.value,
            ReconciliationLine.intent_id.is_not(None),
        )
    )
    return {intent_id: run_id for intent_id, run_id in rows if intent_id}
