"""Configuration de l'application, lue exclusivement depuis l'environnement (12-factor).

Les valeurs par défaut ne servent qu'au développement local et à `docker compose up`
sur une machine propre. Elles sont préfixées par ``dev-only-`` et refusées en production.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal, Self

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

MIN_SECRET_LENGTH = 32  # RFC 7518 §3.2 : clé HMAC-SHA-256 d'au moins 256 bits
DEV_SECRET_PREFIX = "dev-only-"  # noqa: S105 — marqueur, pas un secret

Environment = Literal["development", "test", "production"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="XAALIS_",
        env_file=".env",
        extra="ignore",
        frozen=True,
    )

    environment: Environment = "development"
    service_name: str = "xaalis-api"

    database_url: str = "postgresql+asyncpg://xaalis:xaalis@localhost:5432/xaalis"
    db_pool_size: int = Field(default=20, ge=1)
    db_max_overflow: int = Field(default=10, ge=0)
    db_statement_timeout_ms: int = Field(default=5_000, ge=100)
    db_lock_timeout_ms: int = Field(default=2_000, ge=100)

    redis_url: str = "redis://localhost:6379/0"

    api_key_pepper: SecretStr = SecretStr(f"{DEV_SECRET_PREFIX}api-key-pepper-not-for-production")
    jwt_secret: SecretStr = SecretStr(f"{DEV_SECRET_PREFIX}jwt-secret-not-for-production-use")
    jwt_issuer: str = "xaalis-gateway"
    jwt_audience: str = "xaalis-admin"
    admin_token_ttl_seconds: int = Field(default=3_600, ge=60, le=86_400)

    # Jeu de démonstration appliqué par `python -m xaalis.seed` (service compose `migrate`).
    seed_admin_email: str = "admin@xaalis.example"
    seed_admin_password: SecretStr = SecretStr(f"{DEV_SECRET_PREFIX}admin-password")
    demo_test_publishable_key: SecretStr | None = None
    demo_test_secret_key: SecretStr | None = None

    # XG-13 : limites de débit.
    rate_limit_enabled: bool = True
    rate_limit_per_key_per_minute: int = Field(default=100, ge=1)
    rate_limit_intents_per_payer_per_minute: int = Field(default=20, ge=1)

    log_level: LogLevel = "INFO"
    log_json: bool = True

    @field_validator("api_key_pepper", "jwt_secret")
    @classmethod
    def _secret_is_long_enough(cls, value: SecretStr) -> SecretStr:
        if len(value.get_secret_value()) < MIN_SECRET_LENGTH:
            msg = f"un secret doit compter au moins {MIN_SECRET_LENGTH} caractères"
            raise ValueError(msg)
        return value

    @model_validator(mode="after")
    def _forbid_dev_secrets_in_production(self) -> Self:
        if self.environment != "production":
            return self
        for name in ("api_key_pepper", "jwt_secret", "seed_admin_password"):
            secret: SecretStr = getattr(self, name)
            if secret.get_secret_value().startswith(DEV_SECRET_PREFIX):
                msg = f"XAALIS_{name.upper()} doit être défini explicitement en production"
                raise ValueError(msg)
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
