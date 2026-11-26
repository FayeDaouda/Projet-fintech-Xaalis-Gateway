"""Génère le tableau de bord Grafana provisionné (observability/grafana/dashboards/xaalis.json).

    uv run python scripts/build_dashboard.py           # régénère
    uv run python scripts/build_dashboard.py --check   # CI : le JSON versionné est à jour

Écrire le tableau de bord en Python plutôt qu'à la main dans l'éditeur de Grafana : chaque
panneau est relisible, chaque requête PromQL est revue comme du code.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

OUT = Path(__file__).resolve().parent.parent / "observability/grafana/dashboards/xaalis.json"
PROM = {"type": "prometheus", "uid": "prometheus"}

# Couleurs des opérateurs : mêmes emplacements catégoriels que la console (validés daltonisme).
OPERATOR_COLORS = {"gopay": "#257de0", "teranga": "#f47230", "ndiaga": "#00b894"}


def target(expr: str, legend: str = "", ref: str = "A") -> dict[str, Any]:
    return {"datasource": PROM, "expr": expr, "legendFormat": legend, "refId": ref, "range": True}


def stat(
    title: str,
    expr: str,
    x: int,
    *,
    unit: str = "short",
    desc: str = "",
    thresholds: list[tuple[float | None, str]] | None = None,
) -> dict[str, Any]:
    steps = [{"color": color, "value": value} for value, color in (thresholds or [(None, "green")])]
    return {
        "type": "stat",
        "title": title,
        "description": desc,
        "datasource": PROM,
        "gridPos": {"h": 4, "w": 4, "x": x, "y": 0},
        "targets": [target(expr)],
        "options": {
            "colorMode": "value",
            "graphMode": "area",
            "reduceOptions": {"calcs": ["lastNotNull"]},
        },
        "fieldConfig": {
            "defaults": {
                "unit": unit,
                "decimals": 2 if unit == "s" else 0,
                "thresholds": {"mode": "absolute", "steps": steps},
            },
            "overrides": [],
        },
    }


def series(
    title: str,
    targets: list[dict[str, Any]],
    x: int,
    y: int,
    *,
    unit: str = "short",
    desc: str = "",
    stacked: bool = False,
    w: int = 12,
    overrides: list[dict[str, Any]] | None = None,
    threshold: float | None = None,
    bounds: tuple[int, int] | None = None,
    steps_line: bool = False,
) -> dict[str, Any]:
    custom: dict[str, Any] = {
        "lineWidth": 2,
        "fillOpacity": 12,
        "showPoints": "never",
        "stacking": {"mode": "normal" if stacked else "none"},
    }
    steps: list[dict[str, Any]] = [{"color": "green", "value": None}]
    if threshold is not None:
        custom["thresholdsStyle"] = {"mode": "line+area"}
        steps.append({"color": "red", "value": threshold})
    if steps_line:  # valeurs discrètes (états) : paliers plutôt que courbes interpolées
        custom["lineInterpolation"] = "stepAfter"
    return {
        "type": "timeseries",
        "title": title,
        "description": desc,
        "datasource": PROM,
        "gridPos": {"h": 8, "w": w, "x": x, "y": y},
        "targets": targets,
        "options": {
            "legend": {"displayMode": "list", "placement": "bottom", "showLegend": True},
            "tooltip": {"mode": "multi", "sort": "desc"},
        },
        "fieldConfig": {
            "defaults": {
                "unit": unit,
                "custom": custom,
                "thresholds": {"mode": "absolute", "steps": steps},
                **({"min": bounds[0], "max": bounds[1], "decimals": 0} if bounds else {}),
            },
            "overrides": overrides or [],
        },
    }


def operator_colors() -> list[dict[str, Any]]:
    return [
        {
            "matcher": {"id": "byName", "options": name},
            "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": color}}],
        }
        for name, color in OPERATOR_COLORS.items()
    ]


def row(title: str, y: int) -> dict[str, Any]:
    return {
        "type": "row",
        "title": title,
        "collapsed": False,
        "panels": [],
        "gridPos": {"h": 1, "w": 24, "x": 0, "y": y},
    }


def dashboard() -> dict[str, Any]:
    http = "xaalis_http_request_duration_seconds"
    # Trafic métier seulement : les sondes et la collecte Prometheus faussent débit et latence.
    api = 'route!~"/healthz|/readyz|/metrics"'
    panels: list[dict[str, Any]] = [
        stat("Requêtes / s", f"sum(rate({http}_count{{{api}}}[1m]))", 0, unit="reqps"),
        stat(
            "Latence p95 (API)",
            f"histogram_quantile(0.95, sum by (le) (rate({http}_bucket{{{api}}}[5m])))",
            4,
            unit="s",
            thresholds=[(None, "green"), (0.15, "orange"), (0.5, "red")],
            desc="Objectif de l'énoncé : 150 ms.",
        ),
        stat(
            "Erreurs 5xx / s",
            f'sum(rate({http}_count{{status=~"5.."}}[1m])) or vector(0)',
            8,
            unit="reqps",
            thresholds=[(None, "green"), (0.01, "red")],
        ),
        stat(
            "Webhooks en attente",
            'sum(xaalis_webhook_deliveries{status="pending"}) or vector(0)',
            12,
            thresholds=[(None, "green"), (50, "orange")],
        ),
        stat(
            "Dead letter",
            'sum(xaalis_webhook_deliveries{status="dead_letter"}) or vector(0)',
            16,
            thresholds=[(None, "green"), (1, "red")],
        ),
        stat(
            "Replis anti-fraude (15 min)",
            'sum(increase(xaalis_risk_assessments_total{source="fallback"}[15m])) or vector(0)',
            20,
            thresholds=[(None, "green"), (1, "orange")],
            desc="Service de scoring indisponible ou deadline de 150 ms dépassée (ADR 0013).",
        ),
        row("API", 4),
        series(
            "Débit par route",
            [target(f"sum by (route) (rate({http}_count{{{api}}}[1m]))", "{{route}}")],
            0,
            5,
            unit="reqps",
        ),
        series(
            "Latence p95 par route",
            [
                target(
                    "histogram_quantile(0.95, sum by (le, route) "
                    f"(rate({http}_bucket{{{api}}}[5m])))",
                    "{{route}}",
                )
            ],
            12,
            5,
            unit="s",
            threshold=0.15,
        ),
        row("Paiements et opérateurs", 13),
        series(
            "Intents par statut",
            [target("sum by (status) (xaalis_payment_intents)", "{{status}}")],
            0,
            14,
            stacked=True,
        ),
        series(
            "Disjoncteurs (0 fermé · 1 semi-ouvert · 2 ouvert)",
            [target("max by (operator) (xaalis_circuit_breaker_state)", "{{operator}}")],
            12,
            14,
            overrides=operator_colors(),
            threshold=2,
            bounds=(0, 2),
            steps_line=True,
        ),
        series(
            "Callbacks opérateurs",
            [
                target(
                    "sum by (operator, disposition) (rate(xaalis_operator_callbacks_total[5m]))",
                    "{{operator}} · {{disposition}}",
                )
            ],
            0,
            22,
            unit="ops",
        ),
        series(
            "Écarts du dernier rapprochement",
            [target("sum by (type) (xaalis_reconciliation_discrepancies)", "{{type}}")],
            12,
            22,
            stacked=True,
        ),
        row("Anti-fraude (gRPC)", 30),
        series(
            "Décisions du scoring",
            [
                target(
                    "sum by (decision, source) (rate(xaalis_risk_assessments_total[5m]))",
                    "{{decision}} · {{source}}",
                )
            ],
            0,
            31,
            unit="ops",
            stacked=True,
        ),
        series(
            "Durée des appels gRPC (deadline 150 ms)",
            [
                target(
                    "histogram_quantile(0.95, sum by (le) "
                    "(rate(xaalis_risk_call_duration_seconds_bucket[5m])))",
                    "p95",
                ),
                target(
                    "histogram_quantile(0.99, sum by (le) "
                    "(rate(xaalis_risk_call_duration_seconds_bucket[5m])))",
                    "p99",
                    "B",
                ),
            ],
            12,
            31,
            unit="s",
            threshold=0.15,
        ),
    ]
    for index, panel in enumerate(panels, start=1):
        panel["id"] = index
    return {
        "uid": "xaalis-overview",
        "title": "Xaalis — exploitation",
        "description": "API, paiements, opérateurs, webhooks et anti-fraude. Traces : Jaeger.",
        "tags": ["xaalis"],
        "timezone": "browser",
        "refresh": "10s",
        "time": {"from": "now-30m", "to": "now"},
        "schemaVersion": 39,
        "version": 1,
        "editable": False,
        "graphTooltip": 1,
        "links": [
            {
                "title": "Traces (Jaeger)",
                "type": "link",
                "url": "http://localhost:16686/search?service=xaalis-api",
                "targetBlank": True,
                "icon": "external link",
            },
            {
                "title": "Explorer les traces dans Grafana",
                "type": "link",
                "url": "/explore?left=%7B%22datasource%22:%22jaeger%22%7D",
                "icon": "external link",
            },
        ],
        "panels": panels,
    }


def render() -> str:
    return json.dumps(dashboard(), ensure_ascii=False, indent=2) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    content = render()
    if args.check:
        if not OUT.exists() or OUT.read_text() != content:
            print("tableau de bord périmé : uv run python scripts/build_dashboard.py")  # noqa: T201
            return 1
        return 0
    OUT.write_text(content)
    print(f"écrit : {OUT.relative_to(OUT.parents[3])}")  # noqa: T201
    return 0


if __name__ == "__main__":
    sys.exit(main())
