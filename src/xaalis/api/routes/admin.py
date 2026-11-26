"""Routes d'administration (JWT, rôle admin)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Path
from pydantic import Field, SecretStr

from xaalis.admin.service import authenticate_admin
from xaalis.admin.tokens import issue_admin_token
from xaalis.api.auth import AdminDep
from xaalis.api.pagination import DEFAULT_PAGE_SIZE, LimitQuery, Page, cursor_param
from xaalis.api.problems import Problem, ProblemError
from xaalis.api.resources import ResourcesDep, SessionDep
from xaalis.api.schemas import ApiModel
from xaalis.ledger.health import check_ledger_health
from xaalis.merchants import service as merchants
from xaalis.merchants.models import Merchant

MERCHANT_ID_PATTERN = r"^m_[0-9A-HJKMNP-TV-Z]{26}$"

# Connexion : seule route admin sans jeton, d'où un routeur distinct.
auth_router = APIRouter(prefix="/v1/admin", tags=["admin"])

router = APIRouter(
    prefix="/v1/admin",
    tags=["admin"],
    responses={
        401: {"model": Problem, "description": "Jeton absent ou invalide."},
        403: {"model": Problem, "description": "Rôle administrateur requis."},
    },
)


class LedgerTotals(ApiModel):
    all: int
    test: int
    live: int


class LedgerHealthOut(ApiModel):
    object: Literal["ledger_health"] = "ledger_health"
    balanced: bool = Field(
        description="Vrai si la somme de toutes les écritures est nulle (XG-16)."
    )
    totals: LedgerTotals
    transactions_count: int
    entries_count: int
    unbalanced_transactions: list[str] = Field(
        description="Transactions de somme non nulle ou de moins de deux écritures (100 max)."
    )
    immutability_enforced: bool = Field(
        description="Vrai si tous les triggers d'immutabilité et d'équilibre sont actifs."
    )
    checked_at: datetime


@router.get(
    "/ledger/health",
    response_model=LedgerHealthOut,
    summary="Vérifie l'invariant du grand livre (XG-16)",
)
async def ledger_health(_: AdminDep, session: SessionDep) -> LedgerHealthOut:
    health = await check_ledger_health(session)
    return LedgerHealthOut(
        balanced=health.balanced,
        totals=LedgerTotals(all=health.total, test=health.total_test, live=health.total_live),
        transactions_count=health.transactions_count,
        entries_count=health.entries_count,
        unbalanced_transactions=health.unbalanced_transactions,
        immutability_enforced=health.immutability_enforced,
        checked_at=datetime.now(UTC),
    )


# --------------------------------------------------------------------------- connexion


class AdminLogin(ApiModel):
    # Classes explicites plutôt que \s : JSON Schema (ECMAScript) et Pydantic (Rust) ne
    # mettent pas les mêmes caractères dans \s (ex. U+0085), le contrat divergerait.
    email: str = Field(min_length=3, max_length=254, pattern=r"^[^@ \t\n\r\f\v]+@[^@ \t\n\r\f\v]+$")
    password: SecretStr = Field(min_length=1, max_length=256)


class AdminToken(ApiModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"  # noqa: S105 — type de jeton OAuth 2
    expires_in: int = Field(description="Durée de validité en secondes.")


@auth_router.post(
    "/auth/token",
    response_model=AdminToken,
    summary="Obtenir un JWT administrateur",
    responses={401: {"model": Problem, "description": "Identifiants invalides."}},
)
async def admin_login(body: AdminLogin, session: SessionDep, resources: ResourcesDep) -> AdminToken:
    admin = await authenticate_admin(session, body.email, body.password.get_secret_value())
    if admin is None:
        # Même réponse que l'e-mail soit inconnu ou le mot de passe faux (pas d'énumération).
        raise ProblemError(401, "invalid_credentials", "E-mail ou mot de passe incorrect.")
    settings = resources.settings
    return AdminToken(
        access_token=issue_admin_token(settings, admin.id),
        expires_in=settings.admin_token_ttl_seconds,
    )


# --------------------------------------------------------------------------- marchands


class MerchantCreate(ApiModel):
    name: str = Field(min_length=1, max_length=200, examples=["Boutique Teranga"])


class MerchantOut(ApiModel):
    id: str = Field(examples=["m_01JD8Q3W7Z4K2N6R5T9V0XBCYA"])
    object: Literal["merchant"] = "merchant"
    name: str
    created_at: datetime

    @classmethod
    def from_model(cls, merchant: Merchant) -> MerchantOut:
        return cls(id=merchant.id, name=merchant.name, created_at=merchant.created_at)


class ApiKeyPair(ApiModel):
    publishable_key: str
    secret_key: str


class MerchantCreated(MerchantOut):
    """Réponse de création : les clés en clair ne sont montrées qu'**une seule fois**."""

    publishable_key: str = Field(description="Clé publique de test (`pk_test_…`).")
    secret_key: str = Field(description="Clé secrète de test (`sk_test_…`), affichée une fois.")
    live_keys: ApiKeyPair = Field(description="Clés de l'environnement live, affichées une fois.")


@router.post(
    "/merchants",
    status_code=201,
    response_model=MerchantCreated,
    summary="Créer un marchand (renvoie ses clés une seule fois)",
)
async def create_merchant(
    body: MerchantCreate, _: AdminDep, session: SessionDep, resources: ResourcesDep
) -> MerchantCreated:
    async with session.begin():
        created = await merchants.create_merchant_with_keys(
            session,
            body.name.strip() or body.name,
            pepper=resources.settings.api_key_pepper.get_secret_value(),
        )
    await session.refresh(created.merchant)
    base = MerchantOut.from_model(created.merchant)
    return MerchantCreated(
        **base.model_dump(),
        publishable_key=created.test_keys.publishable_key,
        secret_key=created.test_keys.secret_key,
        live_keys=ApiKeyPair(
            publishable_key=created.live_keys.publishable_key,
            secret_key=created.live_keys.secret_key,
        ),
    )


@router.get("/merchants", response_model=Page[MerchantOut], summary="Lister les marchands")
async def list_merchants(
    _: AdminDep,
    session: SessionDep,
    limit: LimitQuery = DEFAULT_PAGE_SIZE,
    starting_after: Annotated[str | None, cursor_param("m")] = None,
) -> Page[MerchantOut]:
    rows, has_more = await merchants.list_merchants(
        session, limit=limit, starting_after=starting_after
    )
    data = [MerchantOut.from_model(row) for row in rows]
    return Page[MerchantOut](
        data=data, has_more=has_more, next_cursor=data[-1].id if has_more else None
    )


@router.get(
    "/merchants/{merchant_id}",
    response_model=MerchantOut,
    summary="Consulter un marchand",
    responses={404: {"model": Problem, "description": "Marchand inconnu."}},
)
async def get_merchant(
    _: AdminDep,
    session: SessionDep,
    merchant_id: Annotated[str, Path(pattern=MERCHANT_ID_PATTERN)],
) -> MerchantOut:
    merchant = await merchants.get_merchant(session, merchant_id)
    if merchant is None:
        raise ProblemError(404, "not_found", "Marchand inconnu.")
    return MerchantOut.from_model(merchant)
