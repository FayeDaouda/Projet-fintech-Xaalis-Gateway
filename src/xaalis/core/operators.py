"""Opérateurs mobile money pris en charge."""

from __future__ import annotations

from enum import StrEnum


class Operator(StrEnum):
    GOPAY = "gopay"
    TERANGA = "teranga"
    NDIAGA = "ndiaga"
