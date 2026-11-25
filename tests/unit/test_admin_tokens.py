from datetime import UTC, datetime, timedelta

import jwt
import pytest
from pydantic import SecretStr

from xaalis.admin.tokens import (
    InsufficientRoleError,
    InvalidAdminTokenError,
    issue_admin_token,
    verify_admin_token,
)
from xaalis.config import Settings

SETTINGS = Settings(jwt_secret=SecretStr("unit-test-secret-" + "x" * 32))


def test_valid_admin_token_is_accepted() -> None:
    principal = verify_admin_token(SETTINGS, issue_admin_token(SETTINGS, "adm_1"))
    assert principal.subject == "adm_1"


def test_expired_token_is_rejected() -> None:
    token = issue_admin_token(
        SETTINGS, "adm_1", now=datetime.now(UTC) - timedelta(hours=2), ttl=timedelta(hours=1)
    )
    with pytest.raises(InvalidAdminTokenError):
        verify_admin_token(SETTINGS, token)


def test_token_signed_with_another_secret_is_rejected() -> None:
    other = Settings(jwt_secret=SecretStr("another-secret-" + "y" * 32))
    with pytest.raises(InvalidAdminTokenError):
        verify_admin_token(SETTINGS, issue_admin_token(other, "adm_1"))


def test_unsigned_alg_none_token_is_rejected() -> None:
    forged = jwt.encode(
        {"sub": "adm_1", "role": "admin", "iss": SETTINGS.jwt_issuer, "aud": SETTINGS.jwt_audience},
        key=None,
        algorithm="none",
    )
    with pytest.raises(InvalidAdminTokenError):
        verify_admin_token(SETTINGS, forged)


def test_token_without_admin_role_is_forbidden() -> None:
    with pytest.raises(InsufficientRoleError):
        verify_admin_token(SETTINGS, issue_admin_token(SETTINGS, "adm_1", role="support"))
