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


@pytest.mark.unit
def test_email_settings_are_loaded_from_the_approved_environment_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MDM_DATABASE_URL", "postgresql+asyncpg://mdm:secret@localhost/mdm")
    monkeypatch.setenv("MDM_EMAIL_PUBLIC_APP_BASE_URL", "https://app.example.net")
    monkeypatch.setenv("MDM_EMAIL_SMTP_HOST", "smtp.example.net")
    monkeypatch.setenv("MDM_EMAIL_SMTP_PORT", "465")
    monkeypatch.setenv("MDM_EMAIL_SMTP_SECURITY", "TLS")
    monkeypatch.setenv("MDM_EMAIL_SMTP_USERNAME", "smtp-user")
    monkeypatch.setenv("MDM_EMAIL_SMTP_PASSWORD", "smtp-password")
    monkeypatch.setenv("MDM_EMAIL_SENDER_ADDRESS", "noreply@example.net")
    monkeypatch.setenv("MDM_EMAIL_SMTP_TIMEOUT_SECONDS", "4.5")

    settings = Settings(_env_file=None)

    assert str(settings.email_public_app_base_url) == "https://app.example.net/"
    assert settings.email_smtp_host == "smtp.example.net"
    assert settings.email_smtp_port == 465
    assert settings.email_smtp_security == "TLS"
    assert settings.email_smtp_username == "smtp-user"
    assert settings.email_smtp_password is not None
    assert settings.email_smtp_password.get_secret_value() == "smtp-password"
    assert str(settings.email_sender_address) == "noreply@example.net"
    assert settings.email_smtp_timeout_seconds == 4.5
    assert "smtp-password" not in repr(settings)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("SMTP.Example.net.", "smtp.example.net"),
        ("localhost", "localhost"),
        ("127.0.0.1", "127.0.0.1"),
        ("::1", "::1"),
        ("münchen.example", "xn--mnchen-3ya.example"),
        ("smtp.faß.de", "smtp.xn--fa-hia.de"),
        ("smtp.xn--fa-hia.de", "smtp.xn--fa-hia.de"),
        ("smtp.fass.de", "smtp.fass.de"),
        ("smtp.\u03c2.gr", "smtp.xn--3xa.gr"),
        ("smtp.\u03c3.gr", "smtp.xn--4xa.gr"),
    ],
)
def test_email_smtp_host_accepts_and_normalizes_dns_or_ip_literals(
    value: str,
    expected: str,
) -> None:
    settings = Settings(
        database_url="postgresql+asyncpg://mdm:secret@localhost/mdm",
        email_smtp_host=value,
        _env_file=None,
    )

    assert settings.email_smtp_host == expected


@pytest.mark.unit
def test_email_public_app_origin_accepts_a_bracketed_ipv6_authority() -> None:
    settings = Settings(
        database_url="postgresql+asyncpg://mdm:secret@localhost/mdm",
        email_public_app_base_url="https://[2001:db8::1]/",
        _env_file=None,
    )

    assert str(settings.email_public_app_base_url) == "https://[2001:db8::1]/"


@pytest.mark.unit
@pytest.mark.parametrize(
    "override",
    [
        {"email_public_app_base_url": "http://app.example.net"},
        {"email_public_app_base_url": "https://user@app.example.net"},
        {"email_public_app_base_url": "https://app.example.net/path"},
        {"email_public_app_base_url": "https://./"},
        {"email_public_app_base_url": "https://bad..example/"},
        {"email_public_app_base_url": "https://-bad.example/"},
        {"email_smtp_host": "   "},
        {"email_smtp_host": " smtp.example.net"},
        {"email_smtp_host": "https://smtp.example.net"},
        {"email_smtp_host": "smtp.example.net/path"},
        {"email_smtp_host": "not a host"},
        {"email_smtp_host": "bad_host.example.net"},
        {"email_smtp_host": "-smtp.example.net"},
        {"email_smtp_host": "smtp..example.net"},
        {"email_smtp_host": "fe80::1%en0"},
        {"email_smtp_host": "[2001:db8::1]"},
        {"email_smtp_host": "smtp.\u200dexample.net"},
        {"email_smtp_port": 0},
        {"email_smtp_port": 65536},
        {"email_smtp_security": "PLAINTEXT"},
        {"email_smtp_username": ""},
        {"email_smtp_password": ""},
        {"email_sender_address": "not-an-email"},
        {"email_smtp_timeout_seconds": 0},
        {"email_smtp_timeout_seconds": float("inf")},
        {"email_smtp_timeout_seconds": float("nan")},
    ],
)
def test_email_settings_reject_unsafe_or_invalid_values(override: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        Settings(
            database_url="postgresql+asyncpg://mdm:secret@localhost/mdm",
            _env_file=None,
            **override,
        )


@pytest.mark.unit
@pytest.mark.parametrize("missing", ["origins", "hmac", "jwt"])
def test_enabled_sessions_require_complete_configuration(missing: str) -> None:
    from pydantic import ValidationError

    values: dict[str, Any] = {
        "database_url": "postgresql+asyncpg://user:password@localhost/mdm_test",
        "auth_sessions_enabled": True,
        "auth_allowed_origins": ("https://app.example.net",),
        "auth_ip_hmac_secret": "A" * 43,
        "auth_jwt_active_kid": "test-key",
        "auth_jwt_private_key_path": "test-private.pem",
        "auth_jwt_jwks_path": "test-public.json",
    }
    if missing == "origins":
        values["auth_allowed_origins"] = ()
    elif missing == "hmac":
        values["auth_ip_hmac_secret"] = None
    else:
        values["auth_jwt_active_kid"] = None
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **values)
