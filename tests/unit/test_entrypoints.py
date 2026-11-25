"""Chaque point d'entrée doit charger TOUS les modèles ORM, dans un processus vierge.

Régression réelle : le worker n'importait pas ``xaalis.merchants.models`` ; SQLAlchemy ne
pouvait pas résoudre la clé étrangère de ``payment_intents`` et chaque lot d'expiration
échouait — alors que la suite de tests, qui importe tout, restait verte.
"""

import subprocess
import sys

import pytest

ENTRYPOINTS = ["xaalis.worker.__main__", "xaalis.api.app", "xaalis.seed"]


@pytest.mark.parametrize("module", ENTRYPOINTS)
def test_entrypoint_configures_every_mapper_in_a_fresh_process(module: str) -> None:
    code = (
        f"import {module}\n"
        "from xaalis.infra.db import Base\n"
        "for table in Base.metadata.tables.values():\n"
        "    for fk in table.foreign_keys:\n"
        "        fk.column  # lève NoReferencedTableError si la table cible n'est pas chargée\n"
    )
    result = subprocess.run(  # noqa: S603 — commande fixe, sans entrée externe
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60, check=False
    )
    assert result.returncode == 0, result.stderr[-2000:]
