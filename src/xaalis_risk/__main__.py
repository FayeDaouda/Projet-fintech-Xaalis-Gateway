"""Point d'entrée du microservice : ``python -m xaalis_risk``."""

from __future__ import annotations

import asyncio

from xaalis.observability.logging import configure_logging
from xaalis_risk.server import RiskSettings, serve


def main() -> None:
    configure_logging(service="xaalis-risk")
    asyncio.run(serve(RiskSettings()))


if __name__ == "__main__":
    main()
