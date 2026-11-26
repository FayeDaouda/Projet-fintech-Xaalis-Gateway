"""Chiffrement des secrets d'endpoints au repos (Fernet : AES-128-CBC + HMAC-SHA-256).

Contrairement aux clés d'API (empreinte seule, ADR 0004), le secret d'un endpoint doit
être **relu** pour signer chaque envoi : on le chiffre avec une clé de l'environnement.
Une fuite de la base seule ne révèle donc aucun secret de signature.
"""

from __future__ import annotations

import base64
import hashlib
import secrets

from cryptography.fernet import Fernet

SECRET_PREFIX = "whsec_"  # noqa: S105 — préfixe d'affichage, pas un secret


def _fernet(key_material: str) -> Fernet:
    # Clé Fernet (32 octets en base64 URL) dérivée de la clé de l'environnement.
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(key_material.encode()).digest()))


def generate_secret() -> str:
    return SECRET_PREFIX + secrets.token_urlsafe(32)


def encrypt(key_material: str, secret: str) -> bytes:
    return _fernet(key_material).encrypt(secret.encode())


def decrypt(key_material: str, ciphertext: bytes) -> str:
    return _fernet(key_material).decrypt(ciphertext).decode()
