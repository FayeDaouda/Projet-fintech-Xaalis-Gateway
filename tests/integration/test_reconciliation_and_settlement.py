"""Lot 6 de bout en bout : relevé piégé → rapport d'écarts → règlement T+1 → virement."""

import dataclasses
import json
from collections.abc import AsyncIterator
from datetime import date, timedelta
from typing import Any

import httpx
import pytest
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.integration.conftest import bearer, signed_headers, succeeded_intent
from tests.integration.test_operator_flow import Platform, _confirmed, _platform
from xaalis.api.app import create_app
from xaalis.api.resources import Resources
from xaalis.core.clock import today_in_dakar
from xaalis.core.operators import Operator
from xaalis.ledger.health import check_ledger_health
from xaalis.payments.models import PaymentIntent
from xaalis.reconciliation.models import ReconciliationRun
from xaalis.reconciliation.service import process_next
from xaalis.worker.jobs import run_nightly

TODAY = today_in_dakar()


@pytest.fixture
async def platform(app: FastAPI, resources: Resources) -> AsyncIterator[Platform]:
    async for item in _platform(app, resources):
        yield item


TOMORROW = TODAY + timedelta(days=1)


async def _no_fetch(_: Operator, __: date) -> bytes:  # les relevés sont fournis
    raise AssertionError("aucun appel au simulateur attendu")


async def _drain(sessionmaker: async_sessionmaker[AsyncSession], fetch: Any = _no_fetch) -> None:
    while await process_next(sessionmaker, fetch):
        pass


async def _upload(
    client: httpx.AsyncClient, admin_headers: dict[str, str], csv: str, operator: str = "gopay"
) -> httpx.Response:
    return await client.post(
        "/v1/admin/reconciliations",
        params={"date": TODAY.isoformat(), "operator": operator},
        content=csv.encode(),
        headers={**admin_headers, "Content-Type": "text/csv"},
    )


async def _reference(sessionmaker: async_sessionmaker[AsyncSession], intent_id: str) -> str:
    async with sessionmaker() as session:
        ref = await session.scalar(
            select(PaymentIntent.operator_reference).where(PaymentIntent.id == intent_id)
        )
    assert ref is not None
    return ref


async def _report(client: httpx.AsyncClient, admin_headers: dict[str, str]) -> Any:
    response = await client.get(f"/v1/admin/reconciliations/{TODAY}", headers=admin_headers)
    assert response.status_code == 200, response.text
    return response.json()


def _by_reference(report: Any) -> dict[str, str]:
    return {d["operator_reference"]: d["type"] for d in report["discrepancies"]}


# ------------------------------------------------------------------ rapprochement


async def test_simulator_statement_anomalies_are_all_reported(
    client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    onboarded_merchant: dict[str, Any],
    platform: Platform,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Relevé réel du simulateur, avec ses 4 anomalies injectées : chacune est retrouvée."""
    sk = bearer(onboarded_merchant["secret_key"])
    intents = [await _confirmed(client, sk, "78") for _ in range(6)]
    await platform.run()

    async def fetch(operator: Operator, day: date) -> bytes:
        response = await platform.sim_http.get(f"/{operator.value}/statements/{day}.csv")
        return response.content

    accepted = await client.post(
        "/v1/admin/reconciliations",
        json={"date": TODAY.isoformat(), "operators": ["gopay"]},
        headers=admin_headers,
    )
    assert accepted.status_code == 202
    assert accepted.json()["runs"][0]["status"] == "pending"
    await _drain(sessionmaker, fetch)

    injected = (await platform.sim_http.get(f"/statements/{TODAY}/anomalies.json")).json()
    expected_type = {
        "missing": "missing_in_statement",
        "extra": "missing_in_ledger",
        "amount": "amount_mismatch",
        "duplicate": "duplicate",
    }
    found = _by_reference(await _report(client, admin_headers))
    gopay_anomalies = [a for a in injected["anomalies"] if a["operator"] == "gopay"]
    assert len(gopay_anomalies) == 4
    for anomaly in gopay_anomalies:
        assert found[anomaly["operator_reference"]] == expected_type[anomaly["type"]]

    anomalous = {a["operator_reference"] for a in gopay_anomalies}
    for intent_id in intents:
        reference = await _reference(sessionmaker, intent_id)
        if reference not in anomalous:
            assert reference not in found, "une ligne saine est rapprochée sans écart"


async def test_uploaded_statement_report_and_csv_export(
    client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    onboarded_merchant: dict[str, Any],
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    ok = await succeeded_intent(sessionmaker, onboarded_merchant["id"], 10_000)
    wrong = await succeeded_intent(sessionmaker, onboarded_merchant["id"], 7_000)
    ok_ref, wrong_ref = await _reference(sessionmaker, ok), await _reference(sessionmaker, wrong)
    csv = (
        "operator_reference,merchant_reference,amount,currency,status\n"
        f"{ok_ref},{ok},10000,XOF,succeeded\n"
        f"{wrong_ref},{wrong},7700,XOF,succeeded\n"
        "GP-GHOST,pi_X,123,XOF,succeeded\n"
    )
    assert (await _upload(client, admin_headers, csv)).status_code == 202
    await _drain(sessionmaker)

    report = await _report(client, admin_headers)
    found = _by_reference(report)
    assert ok_ref not in found
    assert found[wrong_ref] == "amount_mismatch"
    assert found["GP-GHOST"] == "missing_in_ledger"
    assert report["runs"][0]["source"] == "upload"
    assert report["summary"]["amount_mismatch"] >= 1

    exported = await client.get(
        f"/v1/admin/reconciliations/{TODAY}", params={"format": "csv"}, headers=admin_headers
    )
    assert exported.headers["content-type"].startswith("text/csv")
    header, *rows = exported.text.strip().splitlines()
    assert header.startswith("type,operator,operator_reference")
    assert any(row.startswith(f"amount_mismatch,gopay,{wrong_ref}") for row in rows)


async def test_invalid_statement_fails_the_run_with_a_reason(
    client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    run_id = (
        await _upload(client, admin_headers, "operator_reference,amount\nX,12.5\n", "ndiaga")
    ).json()["runs"][0]["id"]
    await _drain(sessionmaker)
    async with sessionmaker() as session:
        run = await session.get(ReconciliationRun, run_id)
    assert run is not None
    assert (run.status, run.error) == ("failed", "montant non entier dans le relevé")


async def test_reconciliation_requires_admin_and_unknown_date_is_404(
    client: httpx.AsyncClient, admin_headers: dict[str, str]
) -> None:
    assert (
        await client.post("/v1/admin/reconciliations", json={"date": "2026-01-01"})
    ).status_code == 401
    missing = await client.get("/v1/admin/reconciliations/1999-01-01", headers=admin_headers)
    assert missing.status_code == 404


async def test_upload_without_date_and_operator_is_rejected(
    client: httpx.AsyncClient, admin_headers: dict[str, str]
) -> None:
    response = await client.post(
        "/v1/admin/reconciliations",
        content=b"operator_reference,amount\n",
        headers={**admin_headers, "Content-Type": "text/csv"},
    )
    assert response.status_code == 422


# --------------------------------------------------------------- règlement T+1


async def _settle(
    client: httpx.AsyncClient, admin_headers: dict[str, str], *, as_of: date | None
) -> httpx.Response:
    body: dict[str, str] = {"date": TODAY.isoformat()}
    if as_of is not None:
        body["as_of"] = as_of.isoformat()
    return await client.post("/v1/admin/settlements", json=body, headers=admin_headers)


async def test_t_plus_1_settles_only_reconciled_payments_then_payout(
    client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    onboarded_merchant: dict[str, Any],
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    secret = onboarded_merchant["secret_key"]
    clean = await succeeded_intent(sessionmaker, onboarded_merchant["id"], 10_000)
    disputed = await succeeded_intent(sessionmaker, onboarded_merchant["id"], 5_000)
    csv = (
        "operator_reference,amount\n"
        f"{await _reference(sessionmaker, clean)},10000\n"
        f"{await _reference(sessionmaker, disputed)},4000\n"  # montant faux → écart
    )
    await _upload(client, admin_headers, csv)
    await _drain(sessionmaker)

    # Le jour même : interdit (T+1).
    too_early = await _settle(client, admin_headers, as_of=None)
    assert too_early.status_code == 409
    assert too_early.json()["code"] == "settlement_too_early"

    settled = await _settle(client, admin_headers, as_of=TOMORROW)
    assert settled.status_code == 200
    assert disputed in settled.json()["skipped_unreconciled"]
    balance = (await client.get("/v1/balance", headers=bearer(secret))).json()
    # 5 000 − commission minimale de 100 (XG-05) : 4 900 restent en attente (paiement en écart).
    assert (balance["pending"], balance["available"]) == (4_900, 9_820)

    again = (await _settle(client, admin_headers, as_of=TOMORROW)).json()
    assert clean not in again["skipped_unreconciled"]
    assert (await client.get("/v1/balance", headers=bearer(secret))).json()["available"] == 9_820

    body = json.dumps({}).encode()
    payout = await client.post("/v1/payouts", content=body, headers=signed_headers(secret, body))
    assert payout.status_code == 201, payout.text
    assert (payout.json()["object"], payout.json()["amount"], payout.json()["status"]) == (
        "payout",
        9_820,
        "paid",
    )
    balance = (await client.get("/v1/balance", headers=bearer(secret))).json()
    assert (balance["available"], balance["paid_out"]) == (0, 9_820)

    nothing = await client.post("/v1/payouts", content=body, headers=signed_headers(secret, body))
    assert nothing.status_code == 409
    assert nothing.json()["code"] == "insufficient_balance"
    async with sessionmaker() as session:
        assert (await check_ledger_health(session)).balanced


async def test_settlement_after_partial_refund_moves_only_what_is_still_owed(
    client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    onboarded_merchant: dict[str, Any],
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    secret = onboarded_merchant["secret_key"]
    intent_id = await succeeded_intent(sessionmaker, onboarded_merchant["id"], 10_000)
    body = json.dumps({"payment_intent": intent_id, "amount": 3_000}).encode()
    assert (
        await client.post("/v1/refunds", content=body, headers=signed_headers(secret, body))
    ).status_code == 201
    await _upload(
        client,
        admin_headers,
        f"operator_reference,amount\n{await _reference(sessionmaker, intent_id)},10000\n",
    )
    await _drain(sessionmaker)

    await _settle(client, admin_headers, as_of=TOMORROW)
    balance = (await client.get("/v1/balance", headers=bearer(secret))).json()
    assert (balance["pending"], balance["available"]) == (0, 9_820 - 3_000)


async def test_payout_requires_signature(
    client: httpx.AsyncClient, onboarded_merchant: dict[str, Any]
) -> None:
    response = await client.post(
        "/v1/payouts",
        json={},
        headers={**bearer(onboarded_merchant["secret_key"]), "Idempotency-Key": "p1"},
    )
    assert response.status_code == 401


async def test_as_of_override_is_refused_in_production(
    resources: Resources, admin_headers: dict[str, str]
) -> None:
    production = dataclasses.replace(
        resources, settings=resources.settings.model_copy(update={"environment": "production"})
    )
    app = create_app(resources=production)
    async with (
        LifespanManager(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as http,
    ):
        response = await _settle(http, admin_headers, as_of=TOMORROW)
    assert response.status_code == 422


async def test_nightly_requests_yesterdays_reconciliation_once(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    today = date(2031, 3, 2)
    await run_nightly(sessionmaker, today)
    await run_nightly(sessionmaker, today)
    async with sessionmaker() as session:
        count = await session.scalar(
            select(func.count()).where(ReconciliationRun.statement_date == date(2031, 3, 1))
        )
    assert count == len(Operator)
