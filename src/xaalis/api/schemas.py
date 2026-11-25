"""Base commune des schémas d'API : aucun champ inconnu accepté ni divulgué (OWASP API3)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid")
