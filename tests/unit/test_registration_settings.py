from typing import Any

import pytest
from pydantic import ValidationError

from mdm.infrastructure.settings import Settings

pytestmark = pytest.mark.unit


def enabled_settings() -> dict[str, Any]:
    return {
        "database_url": "postgresql+asyncpg://mdm:mdm-local@localhost/mdm_test",
        "auth_sessions_enabled": True,
        "auth_registrations_enabled": True,
        "auth_jwt_active_kid": "test",
        "auth_jwt_private_key_path": "/tmp/test-private.pem",
        "auth_jwt_jwks_path": "/tmp/test-public.json",
        "auth_allowed_origins": ("https://app.example.net",),
        "auth_ip_hmac_secret": "A" * 43,
        "email_public_app_base_url": "https://app.example.net",
        "email_smtp_host": "smtp.example.net",
        "email_smtp_port": 587,
        "email_smtp_username": "test-user",
        "email_smtp_password": "test-secret-not-for-production",
        "email_sender_address": "noreply@example.net",
    }


def test_registration_defaults_to_disabled_without_email_configuration():
    settings = Settings(_env_file=None)
    assert settings.auth_registrations_enabled is False


@pytest.mark.parametrize(
    "missing",
    [
        "auth_sessions_enabled",
        "email_public_app_base_url",
        "email_smtp_host",
        "email_smtp_port",
        "email_smtp_username",
        "email_smtp_password",
        "email_sender_address",
    ],
)
def test_enabled_registration_requires_sessions_and_complete_email_settings(missing: str):
    values = enabled_settings()
    values[missing] = False if missing == "auth_sessions_enabled" else None
    with pytest.raises(ValidationError) as exc:
        Settings(_env_file=None, **values)
    assert "test-secret-not-for-production" not in str(exc.value)


def test_registration_can_be_enabled_with_complete_configuration():
    settings = Settings(_env_file=None, **enabled_settings())
    assert settings.auth_registrations_enabled is True


def test_login_only_configuration_does_not_require_email_settings():
    values = {k: v for k, v in enabled_settings().items() if not k.startswith("email_")}
    values["auth_registrations_enabled"] = False
    settings = Settings(_env_file=None, **values)
    assert settings.auth_sessions_enabled
    assert settings.email_smtp_host is None


@pytest.mark.parametrize(
    ("domains", "expected"),
    [
        ((), ()),
        (("EXAMPLE.net",), ("example.net",)),
        (("münchen.de", "XN--MNCHEN-3YA.DE"), ("xn--mnchen-3ya.de",)),
        (("example.net", "sub.example.net"), ("example.net", "sub.example.net")),
    ],
)
def test_registration_domains_use_the_same_idna_normalization_as_email(domains, expected):
    settings = Settings(_env_file=None, auth_registration_allowed_domains=domains)
    assert settings.auth_registration_allowed_domains == expected


@pytest.mark.parametrize(
    "domain",
    [
        "*",
        "*.example.net",
        "person@example.net",
        "https://example.net",
        "example.net/path",
        "",
        "localhost",
        "127.0.0.1",
        "example.net.",
        " example.net",
    ],
)
def test_registration_domains_reject_non_exact_email_domains(domain):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, auth_registration_allowed_domains=(domain,))
