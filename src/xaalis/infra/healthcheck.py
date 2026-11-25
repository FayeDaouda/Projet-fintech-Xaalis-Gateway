"""Sonde ``HEALTHCHECK`` Docker, sans dépendance à curl dans l'image.

- ``XAALIS_HEALTHCHECK_URL`` : interroge un endpoint HTTP (api, simulateurs) ;
- ``XAALIS_HEALTHCHECK_FILE`` : vérifie la fraîcheur du battement de cœur écrit par
  le worker, qui n'expose pas de port.

Code de sortie 0 = sain, 1 = malade (convention Docker).
"""

from __future__ import annotations

import os
import sys
import time
import urllib.request
from pathlib import Path

DEFAULT_URL = "http://127.0.0.1:8000/healthz"
MAX_HEARTBEAT_AGE_SECONDS = 30.0


def check_url(url: str, timeout: float = 2.0) -> bool:
    if not url.startswith(("http://127.0.0.1", "http://localhost")):
        return False  # la sonde ne parle qu'au processus local
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310 — URL locale vérifiée
            return int(response.status) == 200
    except OSError:
        return False


def check_heartbeat(path: Path, max_age: float = MAX_HEARTBEAT_AGE_SECONDS) -> bool:
    try:
        return (time.time() - path.stat().st_mtime) <= max_age
    except FileNotFoundError:
        return False


def main() -> int:
    heartbeat = os.environ.get("XAALIS_HEALTHCHECK_FILE")
    healthy = (
        check_heartbeat(Path(heartbeat))
        if heartbeat
        else check_url(os.environ.get("XAALIS_HEALTHCHECK_URL", DEFAULT_URL))
    )
    return 0 if healthy else 1


if __name__ == "__main__":
    sys.exit(main())
