"""Pagination par curseur, commune à toutes les listes de l'API.

Les identifiants sont des ULID, donc triables dans le temps : le curseur est simplement
l'identifiant du dernier élément reçu (``starting_after``). Contrairement à ``OFFSET``,
le coût d'une page ne croît pas avec sa position et aucune ligne n'est sautée ou
dupliquée quand de nouveaux objets sont créés pendant le parcours.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import Query
from pydantic import BaseModel, Field

from xaalis.api.schemas import ApiModel

MAX_PAGE_SIZE = 100  # OWASP API4 : taille de page plafonnée
DEFAULT_PAGE_SIZE = 10

LimitQuery = Annotated[
    int,
    Query(ge=1, le=MAX_PAGE_SIZE, description=f"Taille de page, {MAX_PAGE_SIZE} au maximum."),
]


def cursor_param(prefix: str) -> Any:
    """Paramètre ``starting_after`` validé : un identifiant ULID du bon préfixe."""
    return Query(
        pattern=rf"^{prefix}_[0-9A-HJKMNP-TV-Z]{{26}}$",
        description="Identifiant du dernier objet de la page précédente.",
    )


class Page[T: BaseModel](ApiModel):
    object: Literal["list"] = "list"
    data: list[T]
    has_more: bool
    next_cursor: str | None = Field(
        default=None, description="Valeur à passer en `starting_after` pour la page suivante."
    )
