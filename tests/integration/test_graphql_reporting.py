"""Lot 7 : GraphQL du tableau de bord marchand — filtres, agrégats, sécurité, pas de N+1."""

import json
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import httpx
import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.integration.conftest import bearer, signed_headers, succeeded_intent
from xaalis.api.resources import Resources
from xaalis.core.clock import today_in_dakar
from xaalis.core.operators import Operator
from xaalis.operators.callbacks import CallbackOutcome, NormalizedCallback
from xaalis.operators.service import handle_callback
from xaalis.payments.service import Scope, confirm_intent, create_intent

TODAY = today_in_dakar().isoformat()


async def _gql(client: httpx.AsyncClient, key: str, query: str, **variables: Any) -> Any:
    response = await client.post(
        "/graphql", json={"query": query, "variables": variables}, headers=bearer(key)
    )
    assert response.status_code == 200, response.text
    return response.json()


@contextmanager
def count_queries(resources: Resources) -> Iterator[list[str]]:
    statements: list[str] = []

    def record(*args: Any) -> None:
        statements.append(str(args[2]))

    event.listen(resources.engine.sync_engine, "before_cursor_execute", record)
    try:
        yield statements
    finally:
        event.remove(resources.engine.sync_engine, "before_cursor_execute", record)


async def _failed_intent(sessionmaker: async_sessionmaker[AsyncSession], merchant_id: str) -> None:
    scope = Scope(merchant_id, False)
    async with sessionmaker.begin() as session:
        intent = await create_intent(
            session, scope, amount=4_000, currency="XOF", payer_phone="+221789990001", actor="t"
        )
    async with sessionmaker.begin() as session:
        await confirm_intent(session, scope, intent.id, actor="t")
    async with sessionmaker.begin() as session:
        await handle_callback(
            session,
            NormalizedCallback(
                Operator.GOPAY,
                f"GP-{uuid.uuid4().hex}",
                intent.id,
                CallbackOutcome.FAILED,
                4_000,
                {},
            ),
        )


INTENTS_WITH_CHILDREN = """
query($first: Int!) {
  paymentIntents(first: $first) {
    nodes { id status amount payerPhone refunds { id amount } history { fromStatus toStatus } }
  }
}
"""


async def test_no_n_plus_one_query_count_does_not_grow_with_the_number_of_intents(
    client: httpx.AsyncClient,
    onboarded_merchant: dict[str, Any],
    sessionmaker: async_sessionmaker[AsyncSession],
    resources: Resources,
) -> None:
    key = onboarded_merchant["secret_key"]
    for _ in range(10):
        await succeeded_intent(sessionmaker, onboarded_merchant["id"], 10_000)

    counts = {}
    for size in (2, 10):
        with count_queries(resources) as statements:
            body = await _gql(client, key, INTENTS_WITH_CHILDREN, first=size)
        assert len(body["data"]["paymentIntents"]["nodes"]) == size
        counts[size] = len(statements)
    assert counts[2] == counts[10], f"N+1 détecté : {counts}"
    assert counts[10] <= 5  # clé d'API, page, remboursements, historique


async def test_filters_pagination_and_masked_phone(
    client: httpx.AsyncClient,
    onboarded_merchant: dict[str, Any],
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    key = onboarded_merchant["secret_key"]
    for _ in range(3):
        await succeeded_intent(sessionmaker, onboarded_merchant["id"], 5_000)
    await _failed_intent(sessionmaker, onboarded_merchant["id"])

    query = """query($after: String, $d: Date!) { paymentIntents(status: SUCCEEDED,
      operator: GOPAY, first: 2, after: $after, dateFrom: $d, dateTo: $d) {
      nodes { id status operator payerPhone } hasNextPage endCursor } }"""
    first = (await _gql(client, key, query, d=TODAY))["data"]["paymentIntents"]
    assert first["hasNextPage"] is True
    assert {n["status"] for n in first["nodes"]} == {"SUCCEEDED"}
    assert all(n["payerPhone"].startswith("+221 78 *** **") for n in first["nodes"])
    second = (await _gql(client, key, query, d=TODAY, after=first["endCursor"]))["data"][
        "paymentIntents"
    ]
    assert len(second["nodes"]) == 1
    assert not {n["id"] for n in first["nodes"]} & {n["id"] for n in second["nodes"]}


async def test_volumes_success_rate_and_net_revenue(
    client: httpx.AsyncClient,
    onboarded_merchant: dict[str, Any],
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    key = onboarded_merchant["secret_key"]
    await succeeded_intent(sessionmaker, onboarded_merchant["id"], 10_000)  # fee 180
    refunded = await succeeded_intent(sessionmaker, onboarded_merchant["id"], 20_000)  # fee 360
    await _failed_intent(sessionmaker, onboarded_merchant["id"])
    body = json.dumps({"payment_intent": refunded, "amount": 5_000}).encode()
    await client.post("/v1/refunds", content=body, headers=signed_headers(key, body))

    query = """query($d: Date!) {
      dailyVolumes(dateFrom: $d, dateTo: $d) { date operator payments succeeded volume fees }
      successRates(dateFrom: $d, dateTo: $d) { operator finished succeeded successRatePermille }
      revenue(dateFrom: $d, dateTo: $d) { currency gross fees refunded net }
    }"""
    data = (await _gql(client, key, query, d=TODAY))["data"]
    assert data["dailyVolumes"] == [
        {
            "date": TODAY,
            "operator": "GOPAY",
            "payments": 3,
            "succeeded": 2,
            "volume": 30_000,
            "fees": 540,
        }
    ]
    assert data["successRates"] == [
        {"operator": "GOPAY", "finished": 3, "succeeded": 2, "successRatePermille": 666}
    ]
    assert data["revenue"] == {
        "currency": "XOF",
        "gross": 30_000,
        "fees": 540,
        "refunded": 5_000,
        "net": 24_460,
    }


async def test_another_merchants_intent_is_invisible(
    client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    onboarded_merchant: dict[str, Any],
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    other = (
        await client.post("/v1/admin/merchants", json={"name": "Autre"}, headers=admin_headers)
    ).json()
    foreign = await succeeded_intent(sessionmaker, other["id"], 10_000)
    data = await _gql(
        client,
        onboarded_merchant["secret_key"],
        "query($id: ID!) { paymentIntent(id: $id) { id } }",
        id=foreign,
    )
    assert data["data"]["paymentIntent"] is None


async def test_graphql_requires_a_secret_key(
    client: httpx.AsyncClient, onboarded_merchant: dict[str, Any]
) -> None:
    query = {"query": "{ paymentIntents { nodes { id } } }"}
    assert (await client.post("/graphql", json=query)).status_code == 401
    pk = bearer(onboarded_merchant["publishable_key"])
    assert (await client.post("/graphql", json=query, headers=pk)).status_code == 403


@pytest.mark.parametrize(
    ("query", "message"),
    [
        (
            "{ "
            + " ".join(
                f'a{i}: revenue(dateFrom: "2026-01-01", dateTo: "2026-01-01") {{ net }}'
                for i in range(16)
            )
            + " }",
            "alias",
        ),
        ('{ revenue(dateFrom: "2026-02-01", dateTo: "2026-01-01") { net } }', "postérieure"),
        ('{ revenue(dateFrom: "2024-01-01", dateTo: "2026-01-01") { net } }', "366"),
        ("mutation { refund }", ""),
    ],
    ids=["too-many-aliases", "reversed-range", "range-too-long", "no-mutations"],
)
async def test_abusive_or_invalid_queries_are_rejected(
    client: httpx.AsyncClient, onboarded_merchant: dict[str, Any], query: str, message: str
) -> None:
    response = await client.post(
        "/graphql", json={"query": query}, headers=bearer(onboarded_merchant["secret_key"])
    )
    errors = response.json().get("errors")
    assert errors, response.text
    assert message.lower() in json.dumps(errors, ensure_ascii=False).lower()
