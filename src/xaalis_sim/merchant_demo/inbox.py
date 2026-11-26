"""Boîte de réception des webhooks du marchand : vérification, déduplication, journal.

Le journal garde chaque tentative de livraison *authentifiée* (signature valide), y compris
celles refusées pendant une panne simulée : la console montre ainsi, tentative par
tentative, les relances de la passerelle jusqu'à la dead letter. Une requête mal signée
n'entre jamais dans le journal (donnée non authentifiée).
"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

TOLERANCE_SECONDS = 300
JOURNAL_SIZE = 200

Outcome = Literal["accepted", "duplicate", "unavailable"]


def verify_signature(secret: str, header: str, body: bytes, now: int) -> bool:
    parts = dict(item.split("=", 1) for item in header.split(",") if "=" in item)
    timestamp, received = parts.get("t", ""), parts.get("v1", "")
    if not timestamp.isdigit() or abs(now - int(timestamp)) > TOLERANCE_SECONDS:
        return False
    expected = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256)
    return hmac.compare_digest(expected.hexdigest().encode(), received.encode())


@dataclass(slots=True)
class Attempt:
    at: datetime
    outcome: Outcome
    status_code: int


@dataclass(slots=True)
class Entry:
    event_id: str
    type: str
    object_id: str
    object_status: str | None
    created_at: str
    attempts: list[Attempt] = field(default_factory=list)

    @property
    def processed(self) -> bool:
        return any(a.outcome == "accepted" for a in self.attempts)

    def view(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "type": self.type,
            "object_id": self.object_id,
            "object_status": self.object_status,
            "created_at": self.created_at,
            "processed": self.processed,
            "attempts": [
                {"at": a.at.isoformat(), "outcome": a.outcome, "status_code": a.status_code}
                for a in self.attempts
            ],
        }


class WebhookInbox:
    def __init__(self, secrets: list[str]) -> None:
        self.secrets = list(secrets)
        self.failing = False
        self.journal: OrderedDict[str, Entry] = OrderedDict()
        # État le plus récent de chaque objet (l'ordre de livraison n'est pas garanti).
        self.objects: dict[str, dict[str, Any]] = {}

    def add_secret(self, secret: str) -> None:
        if secret not in self.secrets:
            self.secrets.append(secret)

    def authenticate(self, header: str, body: bytes, now: int) -> dict[str, Any] | None:
        if not any(verify_signature(secret, header, body, now) for secret in self.secrets):
            return None
        event: dict[str, Any] = json.loads(body)
        return event

    def record(self, event: dict[str, Any]) -> Outcome:
        """Enregistre une tentative ; ne traite l'événement qu'une fois (``event_id``)."""
        entry = self.journal.get(event["id"])
        if entry is None:
            obj = event["data"]["object"]
            entry = Entry(
                event["id"], event["type"], obj["id"], obj.get("status"), event["created_at"]
            )
            self.journal[entry.event_id] = entry
            while len(self.journal) > JOURNAL_SIZE:
                self.journal.popitem(last=False)
        if self.failing:
            outcome: Outcome = "unavailable"
        elif entry.processed:
            outcome = "duplicate"
        else:
            outcome = "accepted"
            self._apply(event)
        entry.attempts.append(Attempt(datetime.now(UTC), outcome, 503 if self.failing else 200))
        return outcome

    def _apply(self, event: dict[str, Any]) -> None:
        obj = event["data"]["object"]
        current = self.objects.get(obj["id"])
        if current is None or event["created_at"] >= current["event_created_at"]:
            self.objects[obj["id"]] = {
                "status": obj.get("status"),
                "event_created_at": event["created_at"],
            }

    @property
    def processed_count(self) -> int:
        return sum(1 for entry in self.journal.values() if entry.processed)

    def recent(self, limit: int = 50) -> list[dict[str, Any]]:
        return [entry.view() for entry in reversed(self.journal.values())][:limit]
