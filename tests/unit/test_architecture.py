"""Règles d'architecture vérifiées sur le code source.

XG-01 : aucun ``float`` dans le domaine. On analyse l'AST de chaque module de
``xaalis`` : ni littéral flottant, ni appel/annotation ``float``. Seuls quelques
modules techniques (mesure de durées, timeouts) en ont légitimement besoin ; ils
sont listés explicitement ci-dessous et ne manipulent jamais d'argent.
"""

import ast
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parents[2] / "src" / "xaalis"

TECHNICAL_MODULES_ALLOWED_FLOATS = frozenset(
    {
        "api/middleware.py",  # durée des requêtes
        "api/openapi.py",  # bornes flottantes produites par FastAPI, normalisées en entiers
        "api/routes/health.py",  # timeout des sondes
        "infra/healthcheck.py",  # timeout et âge du battement de cœur
        "merchants/key_cache.py",  # horloge monotone du cache des clés
        "observability/metrics.py",  # seuils des histogrammes Prometheus
        "operators/client.py",  # timeouts et backoff des appels réseau
        "worker/runner.py",  # intervalle du battement de cœur
    }
)


def _float_usages(tree: ast.AST) -> list[int]:
    lines = []
    for node in ast.walk(tree):
        is_float_literal = isinstance(node, ast.Constant) and isinstance(node.value, float)
        is_float_name = isinstance(node, ast.Name) and node.id == "float"
        if is_float_literal or is_float_name:
            lines.append(getattr(node, "lineno", 0))
    return lines


def _domain_modules() -> list[Path]:
    return sorted(
        path
        for path in PACKAGE.rglob("*.py")
        if path.relative_to(PACKAGE).as_posix() not in TECHNICAL_MODULES_ALLOWED_FLOATS
    )


@pytest.mark.parametrize(
    "module", _domain_modules(), ids=lambda p: p.relative_to(PACKAGE).as_posix()
)
def test_xg01_no_float_in_domain(module: Path) -> None:
    usages = _float_usages(ast.parse(module.read_text(), filename=str(module)))
    assert not usages, f"float interdit (XG-01) dans {module.name}, lignes {usages}"


def test_allowlist_only_names_existing_modules() -> None:
    for relative in TECHNICAL_MODULES_ALLOWED_FLOATS:
        assert (PACKAGE / relative).is_file(), f"module inexistant dans la liste : {relative}"
