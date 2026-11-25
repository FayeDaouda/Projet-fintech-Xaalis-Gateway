"""Erreurs métier. Le domaine ne connaît pas HTTP : la couche API traduit chaque ``code``
en statut HTTP dans une table unique (``xaalis.api.problems``)."""

from __future__ import annotations

from typing import ClassVar


class DomainError(Exception):
    code: ClassVar[str] = "domain_error"

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class NotFoundError(DomainError):
    code = "not_found"
