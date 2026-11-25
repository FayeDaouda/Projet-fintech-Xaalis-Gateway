import pytest

from xaalis.api.middleware import resolve_request_id


def test_valid_incoming_request_id_is_propagated() -> None:
    assert resolve_request_id("req_0123456789abcdef") == "req_0123456789abcdef"


@pytest.mark.parametrize(
    "raw",
    [None, "", "short", "has space inside!", "x" * 200, "inject\nfake=log"],
)
def test_invalid_request_id_is_replaced(raw: str | None) -> None:
    generated = resolve_request_id(raw)
    assert generated != raw
    assert generated.startswith("req_")
    assert len(generated) == 36
