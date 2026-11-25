"""Comptes administrateurs : création et authentification par mot de passe (Argon2id).

Un mot de passe humain a peu d'entropie : contrairement aux clés d'API (ADR 0004), il
exige un hachage lent et résistant aux GPU. Ce calcul (~50 ms de CPU) est exécuté dans
un thread pour ne pas bloquer la boucle asyncio. On vérifie aussi un hachage factice quand
l'e-mail est inconnu, pour que la durée de réponse ne révèle pas les comptes existants.
"""

from __future__ import annotations

import asyncio
from typing import Final

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from xaalis.admin.models import AdminUser
from xaalis.core.ids import new_id

# Paramètres par défaut d'argon2-cffi (profil RFC 9106 « low memory ») : Argon2id,
# t=3, m=64 Mio, p=4.
_hasher: Final = PasswordHasher()
_DUMMY_HASH: Final = _hasher.hash("xaalis-dummy-password-for-timing")


async def hash_password(password: str) -> str:
    return await asyncio.to_thread(_hasher.hash, password)


def _verify(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerificationError, InvalidHashError):
        return False


def _normalize(email: str) -> str:
    return email.strip().lower()


async def create_admin(session: AsyncSession, email: str, password: str) -> AdminUser:
    password_hash = await hash_password(password)
    admin = AdminUser(id=new_id("adm"), email=_normalize(email), password_hash=password_hash)
    session.add(admin)
    await session.flush()
    return admin


async def find_admin(session: AsyncSession, email: str) -> AdminUser | None:
    return await session.scalar(select(AdminUser).where(AdminUser.email == _normalize(email)))


async def authenticate_admin(session: AsyncSession, email: str, password: str) -> AdminUser | None:
    admin = await find_admin(session, email)
    valid = await asyncio.to_thread(
        _verify, admin.password_hash if admin else _DUMMY_HASH, password
    )
    return admin if valid and admin is not None else None
