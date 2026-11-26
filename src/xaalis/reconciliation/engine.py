"""Moteur de rapprochement (fonctions pures, Polars). Voir ADR 0010.

Format de relevé attendu (CSV, en-tête obligatoire) ::

    operator_reference,merchant_reference,amount,currency,status,occurred_at
    GP-8F3A…,pi_01JD…,10000,XOF,succeeded,2026-11-26T10:15:02Z

Seules ``operator_reference`` et ``amount`` sont indispensables ; ``status`` (si présent)
filtre les lignes ``succeeded`` ; ``currency`` (si présente) doit valoir XOF ; une colonne
``operator`` (relevé consolidé) est filtrée sur l'opérateur rapproché.

Le CSV est lu **entièrement en texte** puis le montant est converti strictement en
``Int64`` : « 12.5 », « 1e3 » ou « abc » font échouer le relevé au lieu d'être arrondis
silencieusement (XG-01 : aucun flottant).
"""

from __future__ import annotations

import io
from enum import StrEnum
from typing import Final

import polars as pl

from xaalis.core.errors import DomainError

MAX_STATEMENT_BYTES: Final = 5 * 1024 * 1024
REQUIRED_COLUMNS: Final = ("operator_reference", "amount")
LEDGER_SCHEMA: Final = {
    "operator_reference": pl.String,
    "intent_id": pl.String,
    "livemode": pl.Boolean,
    "ledger_amount": pl.Int64,
}


class Outcome(StrEnum):
    MATCHED = "matched"
    MISSING_IN_LEDGER = "missing_in_ledger"  # payé selon l'opérateur, absent de nos livres
    MISSING_IN_STATEMENT = "missing_in_statement"  # dans nos livres, absent du relevé
    AMOUNT_MISMATCH = "amount_mismatch"
    DUPLICATE = "duplicate"  # même référence opérateur plusieurs fois dans le relevé


class InvalidStatementError(DomainError):
    code = "invalid_statement"


def parse_statement(raw: bytes, *, operator: str) -> pl.DataFrame:
    """Relevé brut → colonnes ``operator_reference``, ``merchant_reference``, ``amount``."""
    if len(raw) > MAX_STATEMENT_BYTES:
        raise InvalidStatementError("relevé trop volumineux (5 Mo au plus)")
    try:
        frame = pl.read_csv(io.BytesIO(raw), infer_schema=False)
    except (pl.exceptions.PolarsError, UnicodeDecodeError) as exc:
        msg = f"CSV illisible : {type(exc).__name__}"
        raise InvalidStatementError(msg) from None
    frame = frame.rename({name: name.strip().lower() for name in frame.columns})
    missing = [column for column in REQUIRED_COLUMNS if column not in frame.columns]
    if missing:
        msg = f"colonnes manquantes : {', '.join(missing)}"
        raise InvalidStatementError(msg)

    if "operator" in frame.columns:
        frame = frame.filter(pl.col("operator").str.strip_chars() == operator)
    if "status" in frame.columns:
        frame = frame.filter(pl.col("status").str.strip_chars().str.to_lowercase() == "succeeded")
    if "currency" in frame.columns:
        bad = frame.filter(pl.col("currency").str.strip_chars() != "XOF").height
        if bad:
            msg = f"{bad} ligne(s) dans une autre devise que XOF"
            raise InvalidStatementError(msg)
    if "merchant_reference" not in frame.columns:
        frame = frame.with_columns(pl.lit(None, dtype=pl.String).alias("merchant_reference"))

    try:
        return frame.select(
            pl.col("operator_reference").str.strip_chars(),
            pl.col("merchant_reference").str.strip_chars(),
            pl.col("amount").str.strip_chars().cast(pl.Int64, strict=True),
        ).filter(pl.col("operator_reference").is_not_null() & (pl.col("operator_reference") != ""))
    except pl.exceptions.InvalidOperationError:
        raise InvalidStatementError("montant non entier dans le relevé") from None


def ledger_frame(rows: list[dict[str, object]]) -> pl.DataFrame:
    return pl.DataFrame(rows, schema=LEDGER_SCHEMA)


def reconcile(statement: pl.DataFrame, ledger: pl.DataFrame) -> pl.DataFrame:
    """Compare relevé et grand livre ; une ligne par référence opérateur, avec son issue."""
    grouped = statement.group_by("operator_reference").agg(
        pl.len().cast(pl.Int32).alias("occurrences"),
        pl.col("amount").first().alias("statement_amount"),
    )
    joined = grouped.join(ledger, on="operator_reference", how="full", coalesce=True)
    outcome = (
        pl.when(pl.col("ledger_amount").is_null())
        .then(pl.lit(Outcome.MISSING_IN_LEDGER.value))
        .when(pl.col("statement_amount").is_null())
        .then(pl.lit(Outcome.MISSING_IN_STATEMENT.value))
        .when(pl.col("occurrences") > 1)
        .then(pl.lit(Outcome.DUPLICATE.value))
        .when(pl.col("statement_amount") != pl.col("ledger_amount"))
        .then(pl.lit(Outcome.AMOUNT_MISMATCH.value))
        .otherwise(pl.lit(Outcome.MATCHED.value))
    )
    return (
        joined.with_columns(
            outcome.alias("outcome"),
            pl.col("occurrences").fill_null(0),
        )
        .select(
            "outcome",
            "operator_reference",
            "intent_id",
            "livemode",
            "statement_amount",
            "ledger_amount",
            "occurrences",
        )
        .sort("outcome", "operator_reference")
    )
