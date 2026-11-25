"""Classement prudent des réponses opérateur : peut-on basculer sans risque de double débit ?"""

import httpx
import pytest

from xaalis.core.operators import Operator
from xaalis.operators.client import ChargeStatus, OperatorClient, build_http_client
from xaalis.operators.dispatcher import backoff_seconds


def _client(handler: httpx.MockTransport) -> OperatorClient:
    http = httpx.AsyncClient(transport=handler, base_url="http://operators")
    return OperatorClient(http, callback_base_url="http://api:8000/")


async def _charge(client: OperatorClient) -> ChargeStatus:
    result = await client.charge(
        Operator.GOPAY, intent_id="pi_1", amount=1_000, payer_phone="+221781234567", request_id="r1"
    )
    return result.status


@pytest.mark.parametrize(
    ("status_code", "body", "expected"),
    [
        (202, {"operator_reference": "GP-1"}, ChargeStatus.ACCEPTED),
        (200, {"operator_reference": "GP-1"}, ChargeStatus.ACCEPTED),
        (202, {}, ChargeStatus.AMBIGUOUS),
        (503, {}, ChargeStatus.UNAVAILABLE),
        (429, {}, ChargeStatus.UNAVAILABLE),
        (422, {}, ChargeStatus.REJECTED),
        (500, {}, ChargeStatus.AMBIGUOUS),
        (504, {}, ChargeStatus.AMBIGUOUS),
    ],
)
async def test_http_responses_are_classified(
    status_code: int, body: dict[str, str], expected: ChargeStatus
) -> None:
    transport = httpx.MockTransport(lambda _: httpx.Response(status_code, json=body))
    assert await _charge(_client(transport)) is expected


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (httpx.ConnectError("refused"), ChargeStatus.UNAVAILABLE),
        (httpx.ConnectTimeout("slow connect"), ChargeStatus.UNAVAILABLE),
        (httpx.ReadTimeout("lost answer"), ChargeStatus.AMBIGUOUS),
        (httpx.RemoteProtocolError("cut"), ChargeStatus.AMBIGUOUS),
    ],
)
async def test_transport_errors_distinguish_never_sent_from_maybe_processed(
    error: Exception, expected: ChargeStatus
) -> None:
    def raise_error(_: httpx.Request) -> httpx.Response:
        raise error

    assert await _charge(_client(httpx.MockTransport(raise_error))) is expected


async def test_request_carries_reference_callback_url_and_request_id() -> None:
    seen: list[httpx.Request] = []

    def capture(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(202, json={"operator_reference": "GP-1"})

    await _charge(_client(httpx.MockTransport(capture)))
    assert seen[0].url.path == "/gopay/charges"
    assert seen[0].headers["X-Request-Id"] == "r1"
    body = seen[0].read().decode()
    assert '"reference":"pi_1"' in body.replace(" ", "")
    assert "http://api:8000/v1/operators/gopay/callback" in body


def test_backoff_is_exponential_and_capped() -> None:
    assert [backoff_seconds(n) for n in range(1, 9)] == [1, 2, 4, 8, 16, 30, 30, 30]


def test_http_client_uses_configured_timeouts() -> None:
    client = build_http_client("http://operators", connect_ms=500, read_ms=2_000)
    assert client.timeout.connect == 0.5
    assert client.timeout.read == 2
