"""Comportement nominal de chaque opérateur simulé (tableau du lot 3 de l'énoncé)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Profile:
    name: str
    prefixes: frozenset[str]
    latency_ms: tuple[int, int]  # délai avant le callback
    failure_rate: float
    duplicate_rate: float  # probabilité d'envoyer le même callback deux fois
    reference_prefix: str


PROFILES: dict[str, Profile] = {
    "gopay": Profile("gopay", frozenset({"77", "78"}), (1_000, 3_000), 0.05, 0.0, "GP"),
    "teranga": Profile("teranga", frozenset({"76", "70"}), (2_000, 10_000), 0.10, 0.25, "TM"),
    "ndiaga": Profile("ndiaga", frozenset({"75", "77"}), (500, 2_000), 0.03, 0.0, "NC"),
}
