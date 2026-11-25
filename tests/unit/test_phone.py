"""XG-03 : format E.164 sénégalais et masquage dans les réponses et les logs."""

import json
import logging

import pytest
import structlog

from xaalis.core.phone import (
    InvalidPhoneError,
    mask_phone,
    mask_phones_in_text,
    national_prefix,
    normalize_phone,
)
from xaalis.observability.logging import configure_logging


@pytest.mark.parametrize("phone", ["+221771234512", "+221701234567", "+221789999999"])
def test_xg03_valid_senegalese_numbers(phone: str) -> None:
    assert normalize_phone(phone) == phone


@pytest.mark.parametrize(
    "phone",
    [
        "771234512",
        "+22177123451",
        "+2217712345123",
        "+221671234512",
        "+33612345678",
        "+221 77 123 45 12",
        "00221771234512",
        "",
    ],
)
def test_xg03_invalid_numbers_are_rejected(phone: str) -> None:
    with pytest.raises(InvalidPhoneError):
        normalize_phone(phone)


def test_xg03_mask_matches_the_specification_format() -> None:
    assert mask_phone("+221771234512") == "+221 77 *** ** 12"


def test_national_prefix() -> None:
    assert national_prefix("+221761234512") == "76"


@pytest.mark.parametrize("text", ["payer +221771234512 failed", "payer +221 77 123 45 12 failed"])
def test_xg03_numbers_are_masked_in_free_text(text: str) -> None:
    assert mask_phones_in_text(text) == "payer +221 77 *** ** 12 failed"


def test_xg03_numbers_never_reach_the_logs(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(service="test", level="INFO", json_output=True)
    structlog.stdlib.get_logger("t").info(
        "callback +221771234512", payer="+221781112233", nested={"phones": ["+221751112244"]}
    )
    logging.getLogger("stdlib").warning("raw +221761112255")
    output = capsys.readouterr().out
    for raw in ("+221771234512", "+221781112233", "+221751112244", "+221761112255"):
        assert raw not in output
    first = json.loads(output.splitlines()[0])
    assert first["payer"] == "+221 78 *** ** 33"
