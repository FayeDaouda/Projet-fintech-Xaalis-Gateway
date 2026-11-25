"""Gestion des clés d'API par le marchand lui-même."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from fastapi import APIRouter
from pydantic import Field

from xaalis.api.auth import SecretKeyDep
from xaalis.api.problems import Problem
from xaalis.api.resources import ResourcesDep, SessionDep
from xaalis.api.schemas import ApiModel
from xaalis.merchants.service import ROTATION_GRACE_PERIOD, rotate_secret_key

router = APIRouter(
    prefix="/v1/api-keys",
    tags=["api-keys"],
    responses={
        401: {"model": Problem, "description": "Clé absente ou invalide."},
        403: {"model": Problem, "description": "Clé secrète requise."},
    },
)


class PreviousKey(ApiModel):
    id: str
    expires_at: datetime = Field(description="L'ancienne clé reste valide jusqu'à cette date.")


class RotatedKeyOut(ApiModel):
    id: str
    object: Literal["api_key"] = "api_key"
    livemode: bool
    secret_key: str = Field(description="Nouvelle clé secrète, affichée une seule fois.")
    created_at: datetime
    previous_key: PreviousKey


@router.post(
    "/rotate",
    status_code=201,
    response_model=RotatedKeyOut,
    summary="Rotation de la clé secrète",
    description=(
        "Émet une nouvelle clé secrète pour l'environnement de la clé utilisée. "
        f"L'ancienne reste valide {int(ROTATION_GRACE_PERIOD.total_seconds() // 3600)} h, "
        "le temps de déployer la nouvelle."
    ),
)
async def rotate(
    principal: SecretKeyDep, session: SessionDep, resources: ResourcesDep
) -> RotatedKeyOut:
    async with session.begin():
        rotated = await rotate_secret_key(
            session, principal, pepper=resources.settings.api_key_pepper.get_secret_value()
        )
    return RotatedKeyOut(
        id=rotated.key_id,
        livemode=rotated.livemode,
        secret_key=rotated.secret_key,
        created_at=rotated.created_at,
        previous_key=PreviousKey(
            id=rotated.previous_key_id, expires_at=rotated.previous_key_expires_at
        ),
    )
