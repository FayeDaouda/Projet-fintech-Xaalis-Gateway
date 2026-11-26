"""Rapprochement et règlement (administration, JWT admin) — lot 6."""

from __future__ import annotations

import datetime as dt
import io
from collections import Counter
from typing import Annotated, Literal

import polars as pl
from fastapi import APIRouter, Path, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from pydantic import Field, ValidationError

from xaalis.api.auth import AdminDep
from xaalis.api.problems import Problem, ProblemError
from xaalis.api.resources import ResourcesDep, SessionDep
from xaalis.api.schemas import ApiModel
from xaalis.core.clock import today_in_dakar
from xaalis.core.operators import Operator
from xaalis.reconciliation import service as reconciliation
from xaalis.reconciliation.engine import MAX_STATEMENT_BYTES, Outcome
from xaalis.reconciliation.models import ReconciliationRun
from xaalis.settlement.service import settle_day

router = APIRouter(
    prefix="/v1/admin",
    tags=["admin"],
    responses={
        401: {"model": Problem, "description": "Jeton absent ou invalide."},
        403: {"model": Problem, "description": "Rôle administrateur requis."},
    },
)


class ReconciliationRequest(ApiModel):
    date: dt.date
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
    summary="Lancer le rapprochement d'une date (relevé du simulateur ou CSV fourni)",
    description=(
        'Corps JSON `{"date": "2026-11-26", "operators": ["gopay"]}` : le relevé est '
        "récupéré auprès de chaque opérateur. Corps `text/csv` + paramètres `date` et "
        "`operator` : le relevé fourni est rapproché tel quel."
    ),
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": {"$ref": "#/components/schemas/ReconciliationRequest"}
                },
                "text/csv": {"schema": {"type": "string"}},
            },
        }
    },
    responses={422: {"model": Problem, "description": "Requête ou relevé invalide."}},
)
async def request_reconciliation(
    request: Request,
    admin: AdminDep,
    session: SessionDep,
    statement_date: Annotated[dt.date | None, Query(alias="date")] = None,
    operator: Annotated[Operator | None, Query()] = None,
) -> ReconciliationAccepted:
    raw = await request.body()
    if len(raw) > MAX_STATEMENT_BYTES:
        raise ProblemError(413, "payload_too_large", "Relevé de 5 Mo au plus.")
    content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if content_type == "text/csv":
        if statement_date is None or operator is None:
            raise ProblemError(422, "validation_error", "Paramètres date et operator requis.")
        day, operators, csv = statement_date, [operator], raw
    else:
        try:
            payload = ReconciliationRequest.model_validate_json(raw or b"{}")
        except ValidationError as exc:
            raise RequestValidationError(exc.errors(include_url=False)) from None
        day, operators, csv = payload.date, payload.operators, None
    async with session.begin():
        runs = await reconciliation.request_runs(
            session, day, operators=operators, requested_by=admin.subject, statement_csv=csv
        )
    return ReconciliationAccepted(date=day, runs=[_run_out(run) for run in runs])


@router.get(
    "/reconciliations/{statement_date}",
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
    date: dt.date = Field(description="Jour comptable (Dakar) dont on règle les paiements.")
    as_of: dt.date | None = Field(
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
