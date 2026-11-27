"""Journal du grand livre (back-office) : chaque transaction montrée avec ses écritures,
de somme nulle (XG-16), filtrable par marchand et par fait métier, réservé à l'admin."""

from __future__ import annotations

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.integration.conftest import bearer, succeeded_intent
from tests.support import MerchantFactory

URL = "/v1/admin/ledger/transactions"


async def test_xg16_journal_shows_the_double_entry_of_a_payment(
    client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    sessionmaker: async_sessionmaker[AsyncSession],
    make_merchant: MerchantFactory,
) -> None:
    merchant_id = await make_merchant()
    intent_id = await succeeded_intent(sessionmaker, merchant_id, 10_000)

    response = await client.get(URL, params={"reference": intent_id}, headers=admin_headers)

    assert response.status_code == 200, response.text
    (capture,) = response.json()["data"]
    assert (capture["kind"], capture["reference"], capture["total"]) == (
        "payment_capture",
        intent_id,
        0,
    )
    by_kind = {line["account_kind"]: line["amount"] for line in capture["entries"]}
    assert by_kind == {
        "operator_clearing": 10_000,
        "merchant_pending": -9_820,
        "xaalis_revenue": -180,
    }


async def test_journal_filters_by_merchant_and_paginates_newest_first(
    client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    sessionmaker: async_sessionmaker[AsyncSession],
    make_merchant: MerchantFactory,
) -> None:
    merchant_id, other_id = await make_merchant(), await make_merchant()
    first = await succeeded_intent(sessionmaker, merchant_id, 5_000)
    second = await succeeded_intent(sessionmaker, merchant_id, 6_000)
    await succeeded_intent(sessionmaker, other_id, 7_000)

    page = (
        await client.get(
            URL, params={"merchant_id": merchant_id, "limit": 1}, headers=admin_headers
        )
    ).json()
    assert [t["reference"] for t in page["data"]] == [second]
    assert page["has_more"] is True
    rest = (
        await client.get(
            URL,
            params={"merchant_id": merchant_id, "starting_after": page["next_cursor"]},
            headers=admin_headers,
        )
    ).json()
    assert [t["reference"] for t in rest["data"]] == [first]
    assert rest["has_more"] is False


async def test_journal_is_reserved_to_administrators(
    client: httpx.AsyncClient, onboarded_merchant: dict[str, str]
) -> None:
    assert (await client.get(URL)).status_code == 401
    merchant_key = bearer(onboarded_merchant["secret_key"])
    assert (await client.get(URL, headers=merchant_key)).status_code in {401, 403}
