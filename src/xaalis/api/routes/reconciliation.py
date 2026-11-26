"""Rapprochement et règlement (administration, JWT admin) — lot 6."""

from __future__ import annotations

import datetime as dt
import io
from collections import Counter
from typing import Annotated, Literal

import polars as pl
from fastapi import APIRouter, Path, Query, Request, Response
from pydantic import Field
from starlette.convertors import Convertor, register_url_convertor

from xaalis.api.auth import AdminDep
from xaalis.api.problems import Problem, ProblemError
from xaalis.api.resources import ResourcesDep, SessionDep
from xaalis.api.schemas import ApiModel, IsoDate
from xaalis.core.clock import today_in_dakar
from xaalis.core.operators import Operator
from xaalis.reconciliation import service as reconciliation
from xaalis.reconciliation.engine import Outcome
from xaalis.reconciliation.models import ReconciliationRun
from xaalis.settlement.service import settle_day


class IsoDateConvertor(Convertor[dt.date]):
    """Segment de chemin ``AAAA-MM-JJ`` : « /reconciliations/statements » ne peut pas être
    pris pour un rapport daté (sinon 422 au lieu de 405 sur une méthode non déclarée)."""

    regex = r"[0-9]{4}-[0-9]{2}-[0-9]{2}"

    def convert(self, value: str) -> dt.date:
        return dt.date.fromisoformat(value)

    def to_string(self, value: dt.date) -> str:
        return value.isoformat()


register_url_convertor("isodate", IsoDateConvertor())

router = APIRouter(
    prefix="/v1/admin",
    tags=["admin"],
    responses={
        401: {"model": Problem, "description": "Jeton absent ou invalide."},
        403: {"model": Problem, "description": "Rôle administrateur requis."},
    },
)


class ReconciliationRequest(ApiModel):
    date: IsoDate
    operators: list[Operator] = Field(default_factory=lambda: list(Operator), min_length=1)


class RunOut(ApiModel):
    id: str
    operator: Operator
    source: Literal["simulator", "upload"]
    status: Literal["pending", "running", "completed", "failed"]
    statement_lines: int | None
    matched: int | None
    error: str | None
    created_at: dt.datetime
    completed_at: dt.datetime | None


class ReconciliationAccepted(ApiModel):
    object: Literal["reconciliation_request"] = "reconciliation_request"
    date: dt.date
    runs: list[RunOut]


class DiscrepancyOut(ApiModel):
    type: Outcome
    operator: Operator
    operator_reference: str
    intent_id: str | None
    livemode: bool | None
    statement_amount: int | None
    ledger_amount: int | None
    occurrences: int


class ReconciliationReport(ApiModel):
    object: Literal["reconciliation_report"] = "reconciliation_report"
    date: dt.date
    runs: list[RunOut]
    matched: int
    summary: dict[str, int] = Field(description="Nombre d'écarts par type.")
    discrepancies: list[DiscrepancyOut]


def _run_out(run: ReconciliationRun) -> RunOut:
    return RunOut.model_validate(run, from_attributes=True)


@router.post(
    "/reconciliations",
    status_code=202,
    response_model=ReconciliationAccepted,
    summary="Lancer le rapprochement d'une date (relevés récupérés auprès des opérateurs)",
)
async def request_reconciliation(
    body: ReconciliationRequest, admin: AdminDep, session: SessionDep
) -> ReconciliationAccepted:
    async with session.begin():
        runs = await reconciliation.request_runs(
            session, body.date, operators=body.operators, requested_by=admin.subject
        )
    return ReconciliationAccepted(date=body.date, runs=[_run_out(run) for run in runs])


@router.post(
    "/reconciliations/statements",
    status_code=202,
    response_model=ReconciliationAccepted,
    summary="Rapprocher un relevé CSV fourni (relevé piégé, export opérateur…)",
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {"text/csv": {"schema": {"type": "string", "minLength": 1}}},
        }
    },
)
async def upload_statement(
    request: Request,
    admin: AdminDep,
    session: SessionDep,
    statement_date: Annotated[dt.date, Query(alias="date")],
    operator: Annotated[Operator, Query()],
) -> ReconciliationAccepted:
    raw = await request.body()  # taille déjà bornée par le middleware (5 Mio sur ce chemin)
    if not raw.strip():
        raise ProblemError(422, "invalid_statement", "Le relevé CSV est vide.")
    async with session.begin():
        runs = await reconciliation.request_runs(
            session,
            statement_date,
            operators=[operator],
            requested_by=admin.subject,
            statement_csv=raw,
        )
    return ReconciliationAccepted(date=statement_date, runs=[_run_out(run) for run in runs])


@router.get(
    "/reconciliations/{statement_date:isodate}",
    response_model=ReconciliationReport,
    summary="Rapport d'écarts d'une date (JSON, ou CSV avec ?format=csv)",
    responses={
        200: {"content": {"text/csv": {"schema": {"type": "string"}}}},
        404: {"model": Problem, "description": "Aucun rapprochement pour cette date."},
    },
)
async def get_reconciliation(
    statement_date: Annotated[dt.date, Path()],
    _: AdminDep,
    session: SessionDep,
    output: Annotated[Literal["json", "csv"], Query(alias="format")] = "json",
) -> Response | ReconciliationReport:
    report = await reconciliation.report(session, statement_date)
    operator_of = {run.id: Operator(run.operator) for run in report.runs}
    discrepancies = [
        DiscrepancyOut(
            type=Outcome(line.outcome),
            operator=operator_of[line.run_id],
            operator_reference=line.operator_reference,
            intent_id=line.intent_id,
            livemode=line.livemode,
            statement_amount=line.statement_amount,
            ledger_amount=line.ledger_amount,
            occurrences=line.occurrences,
        )
        for line in report.discrepancies
    ]
    if output == "csv":
        frame = pl.DataFrame(
            [d.model_dump(mode="json") for d in discrepancies],
            schema={
                "type": pl.String,
                "operator": pl.String,
                "operator_reference": pl.String,
                "intent_id": pl.String,
                "livemode": pl.Boolean,
                "statement_amount": pl.Int64,
                "ledger_amount": pl.Int64,
                "occurrences": pl.Int64,
            },
        )
        buffer = io.StringIO()
        frame.write_csv(buffer)
        return Response(
            buffer.getvalue(),
            media_type="text/csv",
            headers={
                "Content-Disposition": f'attachment; filename="reconciliation-{statement_date}.csv"'
            },
        )
    return ReconciliationReport(
        date=statement_date,
        runs=[_run_out(run) for run in report.runs],
        matched=sum(1 for line in report.lines if line.outcome == Outcome.MATCHED.value),
        summary=dict(Counter(d.type.value for d in discrepancies)),
        discrepancies=discrepancies,
    )


class SettlementRequest(ApiModel):
    date: IsoDate = Field(description="Jour comptable (Dakar) dont on règle les paiements.")
    as_of: IsoDate | None = Field(
        default=None,
        description="Date « courante » simulée (hors production uniquement), pour la démo.",
    )


class SettlementOut(ApiModel):
    object: Literal["settlement_run"] = "settlement_run"
    date: dt.date
    settled: int
    settled_amount: int
    skipped_unreconciled: list[str]


@router.post(
    "/settlements",
    response_model=SettlementOut,
    summary="Exécuter le règlement T+1 d'un jour comptable",
    responses={409: {"model": Problem, "description": "Trop tôt : règlement à J+1 au plus tôt."}},
)
async def run_settlement(
    body: SettlementRequest, _: AdminDep, resources: ResourcesDep
) -> SettlementOut:
    if body.as_of is not None and resources.settings.environment == "production":
        raise ProblemError(422, "validation_error", "as_of est interdit en production.")
    summary = await settle_day(
        resources.sessionmaker, body.date, as_of=body.as_of or today_in_dakar()
    )
    return SettlementOut(
        date=summary.business_date,
        settled=summary.settled,
        settled_amount=summary.settled_amount,
        skipped_unreconciled=summary.skipped_unreconciled,
    )
