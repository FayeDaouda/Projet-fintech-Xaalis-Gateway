"""OWASP API7 (SSRF) : URL de webhooks interdites en live."""

import pytest

from xaalis.webhooks.ssrf import UnsafeWebhookUrlError, assert_public_destination, validate_url

PRIVATE_URLS = [
    "https://127.0.0.1/hook",
    "https://127.8.9.10/hook",
    "https://10.0.0.5/hook",
    "https://172.16.0.1/hook",
    "https://192.168.1.20/hook",
    "https://169.254.169.254/latest/meta-data",  # métadonnées cloud
    "https://0.0.0.0/hook",
    "https://[::1]/hook",
    "https://[fc00::1]/hook",
    "https://[::ffff:10.0.0.1]/hook",  # IPv4 privée déguisée en IPv6
    "https://localhost/hook",
    "https://api.localhost/hook",
    "https://db.internal/hook",
    "https://printer.local/hook",
]


@pytest.mark.parametrize("url", PRIVATE_URLS)
def test_private_destinations_are_refused_in_live(url: str) -> None:
    with pytest.raises(UnsafeWebhookUrlError):
        validate_url(url, livemode=True)


@pytest.mark.parametrize("url", [*PRIVATE_URLS[:3], "http://merchant-demo:8200/webhooks"])
def test_private_destinations_are_allowed_in_test_mode(url: str) -> None:
    assert validate_url(url, livemode=False) == url


@pytest.mark.parametrize(
    "url",
    [
        "http://boutique.sn/hook",
        "ftp://boutique.sn/hook",
        "https://user:pw@boutique.sn/hook",
        "https:///nohost",
        "boutique.sn/hook",
    ],
)
def test_live_requires_plain_https_urls(url: str) -> None:
    with pytest.raises(UnsafeWebhookUrlError):
        validate_url(url, livemode=True)


def test_public_https_url_is_accepted_in_live() -> None:
    assert validate_url("https://boutique.sn/webhooks", livemode=True)
    assert validate_url("https://8.8.8.8/webhooks", livemode=True)


async def test_dns_rebinding_to_a_private_address_is_blocked_at_send_time() -> None:
    async def rebinding(_: str, __: int) -> list[str]:
        return ["93.184.216.34", "10.0.0.7"]  # une seule adresse privée suffit à refuser

    with pytest.raises(UnsafeWebhookUrlError, match="non publique"):
        await assert_public_destination("https://boutique.sn/hook", rebinding)


async def test_public_resolution_is_allowed() -> None:
    async def public(_: str, __: int) -> list[str]:
        return ["93.184.216.34"]

    await assert_public_destination("https://boutique.sn/hook", public)


async def test_unresolvable_host_is_blocked() -> None:
    async def failing(_: str, __: int) -> list[str]:
        raise OSError("NXDOMAIN")

    with pytest.raises(UnsafeWebhookUrlError, match="DNS"):
        await assert_public_destination("https://nope.invalid/hook", failing)
