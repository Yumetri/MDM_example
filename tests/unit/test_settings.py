from typing import Any

import pytest
from pydantic import ValidationError

from mdm.infrastructure.settings import Settings


@pytest.mark.unit
def test_database_url_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MDM_DATABASE_URL", raising=False)

    with pytest.raises(ValidationError):
        Settings(_env_file=None)


@pytest.mark.unit
def test_database_url_is_hidden_from_repr() -> None:
    settings = Settings(
        database_url="postgresql+asyncpg://mdm:secret@localhost:5432/mdm",
        _env_file=None,
    )

    assert "secret" not in repr(settings)


@pytest.mark.unit
def test_password_hash_settings_use_the_contract_defaults() -> None:
    settings = Settings(
        database_url="postgresql+asyncpg://mdm:secret@localhost:5432/mdm",
        _env_file=None,
    )

    assert settings.auth_password_hash_memory_mib == 19
    assert settings.auth_password_hash_iterations == 2
    assert settings.auth_password_hash_parallelism == 1
    assert settings.auth_password_hash_max_concurrency == 2
    assert settings.auth_password_hash_acquire_timeout_seconds == 2.0


@pytest.mark.unit
@pytest.mark.parametrize(
    "override",
    [
        {"auth_password_hash_memory_mib": 0},
        {"auth_password_hash_iterations": 0},
        {"auth_password_hash_parallelism": 0},
        {"auth_password_hash_max_concurrency": 0},
        {"auth_password_hash_max_concurrency": 9},
        {"auth_password_hash_acquire_timeout_seconds": 0.09},
        {"auth_password_hash_acquire_timeout_seconds": 10.01},
    ],
)
def test_password_hash_settings_reject_invalid_bounds(override: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        Settings(
            database_url="postgresql+asyncpg://mdm:secret@localhost:5432/mdm",
            _env_file=None,
            **override,
        )


@pytest.mark.unit
def test_jwt_file_settings_are_loaded_from_the_approved_environment_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MDM_DATABASE_URL", "postgresql+asyncpg://mdm:secret@localhost/mdm")
    monkeypatch.setenv("MDM_AUTH_JWT_ACTIVE_KID", "key-b")
    monkeypatch.setenv("MDM_AUTH_JWT_PRIVATE_KEY_PATH", "/run/secrets/mdm/jwt-private.pem")
    monkeypatch.setenv("MDM_AUTH_JWT_JWKS_PATH", "/etc/mdm/jwt-public-keys.json")

    settings = Settings(_env_file=None)

    assert settings.auth_jwt_active_kid == "key-b"
    assert str(settings.auth_jwt_private_key_path) == "/run/secrets/mdm/jwt-private.pem"
    assert str(settings.auth_jwt_jwks_path) == "/etc/mdm/jwt-public-keys.json"
