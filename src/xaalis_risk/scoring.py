"""Règles de scoring : fonction pure, entiers uniquement (XG-01), chaque point expliqué.

Le score est une somme de signaux plafonnée à 1000 ‰ ; chaque signal laisse un code
« raison » stable, montré au marchand. Les seuils sont volontairement simples et
documentés : un modèle opaque serait impossible à justifier devant un marchand refusé.

| Signal                                   | Points | Raison               |
|------------------------------------------|--------|----------------------|
| payeur sur liste noire                   | 1000   | payer_blocklisted    |
| montant ≥ 1 000 000 XOF                  | 350    | amount_very_high     |
| montant ≥ 500 000 XOF                    | 200    | amount_high          |
| montant ≥ 200 000 XOF                    | 80     | amount_elevated      |
| ≥ 10 demandes du payeur en 10 min        | 450    | velocity_extreme     |
| ≥ 5 demandes du payeur en 10 min         | 250    | velocity_high        |
| ≥ 3 demandes du payeur en 10 min         | 100    | velocity_elevated    |
| entre 0 h et 5 h, heure de Dakar         | 100    | night_time           |
| montant rond (multiple de 100 000 XOF)   | 50     | round_amount         |

Décision : ≥ 700 ‰ refus, ≥ 400 ‰ revue (le paiement continue, signalé), sinon accord.
"""

from __future__ import annotations

import hashlib
import time
from collections import deque
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

MAX_SCORE: Final = 1_000
DECLINE_THRESHOLD: Final = 700
REVIEW_THRESHOLD: Final = 400
VELOCITY_WINDOW_SECONDS: Final = 600
NIGHT_HOURS: Final = range(0, 5)

AMOUNT_TIERS: Final = (
    (1_000_000, 350, "amount_very_high"),
    (500_000, 200, "amount_high"),
    (200_000, 80, "amount_elevated"),
)
VELOCITY_TIERS: Final = (
    (10, 450, "velocity_extreme"),
    (5, 250, "velocity_high"),
    (3, 100, "velocity_elevated"),
)


class Decision(StrEnum):
    APPROVE = "approve"
    REVIEW = "review"
    DECLINE = "decline"


@dataclass(frozen=True, slots=True)
class Assessment:
    score_permille: int
    decision: Decision
    reasons: tuple[str, ...]


def decide(score: int) -> Decision:
    if score >= DECLINE_THRESHOLD:
        return Decision.DECLINE
    if score >= REVIEW_THRESHOLD:
        return Decision.REVIEW
    return Decision.APPROVE


def assess(*, amount: int, recent_requests: int, dakar_hour: int, blocklisted: bool) -> Assessment:
    """Score d'un paiement. ``recent_requests`` : demandes du même payeur dans la fenêtre,
    celle-ci exclue."""
    if blocklisted:
        return Assessment(MAX_SCORE, Decision.DECLINE, ("payer_blocklisted",))
    points = 0
    reasons: list[str] = []
    for threshold, value, reason in AMOUNT_TIERS:
        if amount >= threshold:
            points += value
            reasons.append(reason)
            break
    for threshold, value, reason in VELOCITY_TIERS:
        if recent_requests >= threshold:
            points += value
            reasons.append(reason)
            break
    if dakar_hour in NIGHT_HOURS:
        points += 100
        reasons.append("night_time")
    if amount >= 100_000 and amount % 100_000 == 0:
        points += 50
        reasons.append("round_amount")
    score = min(points, MAX_SCORE)
    return Assessment(score, decide(score), tuple(reasons))


def payer_fingerprint(phone: str) -> str:
    """Le service ne garde jamais un numéro en clair, même en mémoire (XG-03)."""
    return hashlib.sha256(phone.encode()).hexdigest()


class VelocityTracker:
    """Demandes récentes par payeur, en mémoire (fenêtre glissante de 10 minutes).

    Un seul processus suffit pour ce service ; à plusieurs instances, ces compteurs
    iraient dans Redis comme ceux de la limitation de débit (ADR 0006).
    """

    def __init__(self, window_seconds: int = VELOCITY_WINDOW_SECONDS) -> None:
        self._window = window_seconds
        self._seen: dict[str, deque[int]] = {}

    def hit(self, fingerprint: str, now: int | None = None) -> int:
        """Enregistre une demande ; renvoie le nombre de demandes précédentes dans la fenêtre."""
        moment = int(time.monotonic()) if now is None else now
        history = self._seen.setdefault(fingerprint, deque())
        while history and history[0] <= moment - self._window:
            history.popleft()
        previous = len(history)
        history.append(moment)
        if len(self._seen) > 50_000:  # borne mémoire : on oublie les payeurs inactifs
            self._seen = {
                k: v for k, v in self._seen.items() if v and v[-1] > moment - self._window
            }
        return previous
