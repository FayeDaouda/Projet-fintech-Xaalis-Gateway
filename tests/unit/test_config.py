import pytest
from pydantic import SecretStr, ValidationError

from xaalis.config import Settings


def test_development_accepts_dev_only_defaults() -> None:
    settings = Settings(environment="development")
    assert settings.api_key_pepper.get_secret_value().startswith("dev-only-")


def test_production_refuses_dev_only_secrets() -> None:
    with pytest.raises(ValidationError, match="XAALIS_API_KEY_PEPPER"):
        Settings(environment="production")


def test_production_accepts_explicit_secrets() -> None:
    settings = Settings(
        environment="production",
        api_key_pepper=SecretStr("a" * 48),
        jwt_secret=SecretStr("b" * 48),
        seed_admin_password=SecretStr("an-explicit-admin-password"),
        operator_secret_gopay=SecretStr("g" * 40),
        operator_secret_teranga=SecretStr("t" * 40),
        operator_secret_ndiaga=SecretStr("n" * 40),
    )
    assert settings.environment == "production"
    assert settings.operator_secret("gopay") == "g" * 40


def test_secrets_are_never_rendered_in_repr() -> None:
    settings = Settings(api_key_pepper=SecretStr("super-secret-pepper-" + "z" * 20))
    assert "super-secret-pepper" not in repr(settings)


@pytest.mark.parametrize("field", ["api_key_pepper", "jwt_secret"])
def test_short_secrets_are_refused(field: str) -> None:
    with pytest.raises(ValidationError, match="32 caractères"):
        Settings(**{field: SecretStr("trop-court")})


def test_production_refuses_the_dev_admin_password() -> None:
    with pytest.raises(ValidationError, match="XAALIS_SEED_ADMIN_PASSWORD"):
        Settings(
            environment="production",
            api_key_pepper=SecretStr("a" * 48),
            jwt_secret=SecretStr("b" * 48),
        )


def test_production_refuses_dev_operator_secrets() -> None:
    with pytest.raises(ValidationError, match="XAALIS_OPERATOR_SECRET_GOPAY"):
        Settings(
            environment="production",
            api_key_pepper=SecretStr("a" * 48),
            jwt_secret=SecretStr("b" * 48),
            seed_admin_password=SecretStr("an-explicit-admin-password"),
        )
