"""Protection SSRF des URL de webhooks (OWASP API7).

En **live**, une URL ne peut viser ni le réseau interne ni la machine elle-même :
boucle locale (127.0.0.0/8, ::1), réseaux privés (10/8, 172.16/12, 192.168/16, fc00::/7),
lien local (169.254/16 — métadonnées cloud), adresses réservées, ``localhost`` et suffixes
internes. HTTPS est exigé. Le contrôle est fait deux fois :

1. à l'enregistrement, sur l'URL telle qu'écrite ;
2. **à chaque envoi**, sur les adresses réellement résolues par DNS — sinon un nom public
   pourrait être redirigé vers une IP interne après coup (*DNS rebinding*).

En **test**, les URL privées sont permises (marchand de démo sur le réseau Docker).
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable, Iterable
from typing import Final
from urllib.parse import urlsplit

from xaalis.core.errors import DomainError

_BLOCKED_HOSTNAMES: Final = frozenset({"localhost", "localhost.localdomain", "ip6-localhost"})
_BLOCKED_SUFFIXES: Final = (".localhost", ".local", ".internal", ".lan", ".home.arpa")

Resolver = Callable[[str, int], Awaitable[list[str]]]


class UnsafeWebhookUrlError(DomainError):
    code = "unsafe_webhook_url"


def _is_public(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


def validate_url(url: str, *, livemode: bool) -> str:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise UnsafeWebhookUrlError("URL de webhook invalide : http(s)://hôte/chemin attendu")
    if parts.username or parts.password:
        raise UnsafeWebhookUrlError("les identifiants dans l'URL sont interdits")
    if not livemode:
        return url
    if parts.scheme != "https":
        raise UnsafeWebhookUrlError("HTTPS est obligatoire pour un webhook live")
    host = parts.hostname.lower().rstrip(".")
    if host in _BLOCKED_HOSTNAMES or host.endswith(_BLOCKED_SUFFIXES):
        raise UnsafeWebhookUrlError("une URL locale ou interne est interdite en live")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return url  # nom d'hôte : vérifié après résolution, à chaque envoi
    if not _is_public(host):
        raise UnsafeWebhookUrlError("une adresse IP privée ou réservée est interdite en live")
    return url


async def system_resolver(host: str, port: int) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]


async def assert_public_destination(url: str, resolver: Resolver = system_resolver) -> None:
    """À l'envoi, en live : toutes les adresses résolues doivent être publiques."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    port = parts.port or (443 if parts.scheme == "https" else 80)
    try:
        addresses: Iterable[str] = await resolver(host, port)
    except OSError as exc:
        msg = f"résolution DNS impossible pour {host}"
        raise UnsafeWebhookUrlError(msg) from exc
    if not addresses or not all(_is_public(address) for address in addresses):
        raise UnsafeWebhookUrlError(f"{host} résout vers une adresse non publique")
