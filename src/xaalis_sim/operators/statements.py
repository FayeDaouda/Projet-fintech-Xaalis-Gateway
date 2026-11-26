"""Relevés quotidiens des opérateurs, avec anomalies injectées volontairement (lot 6).

Pour chaque opérateur et chaque jour, à partir des charges réussies ce jour-là (UTC, soit
l'heure de Dakar), on applique de façon **déterministe** (graine = opérateur + date) :

- ``missing``   : une ligne retirée        → la passerelle doit signaler missing_in_statement ;
- ``extra``     : une ligne inventée       → missing_in_ledger ;
- ``amount``    : un montant faussé        → amount_mismatch ;
- ``duplicate`` : une ligne en double      → duplicate.

Avec moins de 4 charges, seules les anomalies possibles sont injectées (``extra`` toujours).
"""

from __future__ import annotations

import csv
import io
import random
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Protocol

COLUMNS = [
    "operator",
    "operator_reference",
    "merchant_reference",
    "amount",
    "currency",
    "status",
    "occurred_at",
]


class ChargeLike(Protocol):
    operator: str
    reference: str
    operator_reference: str
    amount: int
    status: str
    completed_at: Any


@dataclass
class Statement:
    csv: str
    anomalies: list[dict[str, Any]] = field(default_factory=list)


def _row(charge: ChargeLike) -> dict[str, Any]:
    return {
        "operator": charge.operator,
        "operator_reference": charge.operator_reference,
        "merchant_reference": charge.reference,
        "amount": charge.amount,
        "currency": "XOF",
        "status": "succeeded",
        "occurred_at": charge.completed_at.isoformat().replace("+00:00", "Z"),
    }


def _inject(operator: str, day: date, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rng = random.Random(f"{operator}:{day.isoformat()}")  # noqa: S311 — déterminisme voulu
    anomalies: list[dict[str, Any]] = []
    pool = list(range(len(rows)))
    rng.shuffle(pool)
    if len(pool) >= 2:  # garder au moins une ligne saine
        index = pool.pop()
        anomalies.append(
            {"type": "missing", "operator_reference": rows[index]["operator_reference"]}
        )
        rows[index] = {}
    if pool:
        index = pool.pop()
        rows[index] = {**rows[index], "amount": rows[index]["amount"] + 500}
        anomalies.append(
            {"type": "amount", "operator_reference": rows[index]["operator_reference"]}
        )
    if pool:
        index = pool.pop()
        rows.append(dict(rows[index]))
        anomalies.append(
            {"type": "duplicate", "operator_reference": rows[index]["operator_reference"]}
        )
    fake = f"{operator[:2].upper()}-FAKE{rng.getrandbits(32):08X}"  # même relevé à chaque appel
    rows.append(
        {
            "operator": operator,
            "operator_reference": fake,
            "merchant_reference": "pi_UNKNOWN",
            "amount": rng.randint(100, 50_000),
            "currency": "XOF",
            "status": "succeeded",
            "occurred_at": f"{day.isoformat()}T12:00:00Z",
        }
    )
    anomalies.append({"type": "extra", "operator_reference": fake})
    return anomalies


def build_statement(
    charges: list[ChargeLike], operators: list[str], day: date, *, inject: bool
) -> Statement:
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=COLUMNS, lineterminator="\n")
    writer.writeheader()
    statement = Statement(csv="")
    for operator in operators:
        rows = [
            _row(charge)
            for charge in sorted(charges, key=lambda c: c.completed_at or 0)
            if charge.operator == operator
            and charge.status == "succeeded"
            and charge.completed_at is not None
            and charge.completed_at.date() == day
        ]
        if inject:
            for anomaly in _inject(operator, day, rows):
                statement.anomalies.append({"operator": operator, **anomaly})
        writer.writerows(row for row in rows if row)
    statement.csv = output.getvalue()
    return statement
