"""Cas d'usage marchands : création, clés d'API, authentification, rotation."""

from __future__ import annotations

import hmac
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from xaalis.core.ids import new_id
from xaalis.merchants.keys import KeyKind, fingerprint, generate_key, parse_key, redact
from xaalis.merchants.models import ApiKey, Merchant, MerchantEnvironment

ROTATION_GRACE_PERIOD: Final = timedelta(hours=24)  # lot 1 : l'ancienne clé vit encore 24 h


@dataclass(frozen=True, slots=True)
class KeyPair:
    publishable_key: str
    secret_key: str


@dataclass(frozen=True, slots=True)
class CreatedMerchant:
    merchant: Merchant
    test_keys: KeyPair
    live_keys: KeyPair


@dataclass(frozen=True, slots=True)
class MerchantPrincipal:
    """Identité d'un appelant authentifié par clé d'API."""

    merchant_id: str
    livemode: bool
    key_id: str
    kind: KeyKind


@dataclass(frozen=True, slots=True)
class RotatedKey:
    key_id: str
    secret_key: str
    livemode: bool
    created_at: datetime
    previous_key_id: str
    previous_key_expires_at: datetime


async def create_merchant(session: AsyncSession, name: str) -> Merchant:
    """Crée un marchand et ses deux environnements étanches (test et live)."""
    merchant = Merchant(id=new_id("m"), name=name)
    session.add(merchant)
    await session.flush()
    session.add_all(
        [
            MerchantEnvironment(merchant_id=merchant.id, livemode=False),
            MerchantEnvironment(merchant_id=merchant.id, livemode=True),
        ]
    )
    await session.flush()
    return merchant


async def create_merchant_with_keys(
    session: AsyncSession, name: str, *, pepper: str
) -> CreatedMerchant:
    merchant = await create_merchant(session, name)
    test_keys = await _issue_key_pair(session, merchant.id, livemode=False, pepper=pepper)
    live_keys = await _issue_key_pair(session, merchant.id, livemode=True, pepper=pepper)
    return CreatedMerchant(merchant=merchant, test_keys=test_keys, live_keys=live_keys)


async def issue_key(
    session: AsyncSession,
    merchant_id: str,
    *,
    kind: KeyKind,
    livemode: bool,
    pepper: str,
    raw_key: str | None = None,
) -> tuple[ApiKey, str]:
    """Crée une clé et renvoie ``(ligne, clé en clair)`` ; la clé en clair n'est jamais stockée.

    ``raw_key`` permet au jeu de démonstration d'imposer une clé fournie par l'environnement.
    """
    raw = raw_key or generate_key(kind, livemode=livemode)
    parsed = parse_key(raw)
    if parsed is None or parsed.kind is not kind or parsed.livemode is not livemode:
        msg = "clé fournie de forme ou d'environnement incorrect"
        raise ValueError(msg)
    row = ApiKey(
        id=new_id("key"),
        merchant_id=merchant_id,
        livemode=livemode,
        kind=kind.value,
        fingerprint=fingerprint(pepper, raw),
        redacted=redact(raw),
    )
    session.add(row)
    await session.flush()
    return row, raw


async def _issue_key_pair(
    session: AsyncSession, merchant_id: str, *, livemode: bool, pepper: str
) -> KeyPair:
    _, publishable = await issue_key(
        session, merchant_id, kind=KeyKind.PUBLISHABLE, livemode=livemode, pepper=pepper
    )
    _, secret = await issue_key(
        session, merchant_id, kind=KeyKind.SECRET, livemode=livemode, pepper=pepper
    )
    return KeyPair(publishable_key=publishable, secret_key=secret)


async def authenticate_key(
    session: AsyncSession, raw_key: str, *, pepper: str
) -> MerchantPrincipal | None:
    """Résout une clé présentée ; ``None`` si inconnue, expirée, révoquée ou incohérente."""
    parsed = parse_key(raw_key)
    if parsed is None:
        return None
    digest = fingerprint(pepper, raw_key)
    row = await session.scalar(
        select(ApiKey).where(
            ApiKey.fingerprint == digest,
            ApiKey.revoked_at.is_(None),
            or_(ApiKey.expires_at.is_(None), ApiKey.expires_at > func.now()),
        )
    )
    if row is None:
        return None
    # Défense en profondeur : comparaison en temps constant et cohérence avec le préfixe.
    consistent = row.kind == parsed.kind.value and row.livemode is parsed.livemode
    if not (hmac.compare_digest(row.fingerprint, digest) and consistent):
        return None
    return MerchantPrincipal(
        merchant_id=row.merchant_id, livemode=row.livemode, key_id=row.id, kind=parsed.kind
    )


async def rotate_secret_key(
    session: AsyncSession, principal: MerchantPrincipal, *, pepper: str
) -> RotatedKey:
    """Émet une nouvelle clé secrète ; la clé utilisée pour l'appel expire dans 24 h.

    Si la clé courante expirait déjà plus tôt (rotation précédente), on ne prolonge pas
    sa durée de vie : on garde l'échéance la plus proche.
    """
    current = await session.get(ApiKey, principal.key_id, with_for_update={"key_share": True})
    if current is None:  # pragma: no cover — la clé vient d'être authentifiée
        msg = f"clé {principal.key_id} introuvable"
        raise LookupError(msg)

    now = await session.scalar(select(func.now()))
    assert now is not None  # noqa: S101 — now() ne renvoie jamais NULL
    deadline = now + ROTATION_GRACE_PERIOD
    current.expires_at = min(current.expires_at, deadline) if current.expires_at else deadline

    new_row, raw = await issue_key(
        session,
        principal.merchant_id,
        kind=KeyKind.SECRET,
        livemode=principal.livemode,
        pepper=pepper,
    )
    await session.flush()
    await session.refresh(new_row, ["created_at"])
    return RotatedKey(
        key_id=new_row.id,
        secret_key=raw,
        livemode=principal.livemode,
        created_at=new_row.created_at,
        previous_key_id=current.id,
        previous_key_expires_at=current.expires_at,
    )


async def get_merchant(session: AsyncSession, merchant_id: str) -> Merchant | None:
    return await session.get(Merchant, merchant_id)


async def list_merchants(
    session: AsyncSession, *, limit: int, starting_after: str | None
) -> tuple[Sequence[Merchant], bool]:
    """Page de marchands, du plus récent au plus ancien (les ULID sont triables)."""
    query = select(Merchant).order_by(Merchant.id.desc()).limit(limit + 1)
    if starting_after is not None:
        query = query.where(Merchant.id < starting_after)
    rows = (await session.scalars(query)).all()
    return rows[:limit], len(rows) > limit
