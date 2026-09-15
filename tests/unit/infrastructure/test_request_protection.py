import base64
import hashlib
import hmac
from ipaddress import ip_address

import pytest
from pydantic import SecretStr, ValidationError

from mdm.auth_protection import build_auth_protection
from mdm.infrastructure.request_protection import HmacClientIpKey, UnresolvedClientIpResolver
from mdm.infrastructure.settings import Settings

SECRET = base64.urlsafe_b64encode(b"k" * 32).rstrip(b"=").decode()


@pytest.mark.unit
def test_ip_key_uses_canonical_address_and_secret_hmac_without_exposing_material() -> None:
    key = HmacClientIpKey(SecretStr(SECRET))
    digest = hmac.new(b"k" * 32, b"2001:db8::1", hashlib.sha256).hexdigest()
    assert key(ip_address("2001:0db8:0:0:0:0:0:1")) == "mdm:rate-limit:auth:" + digest
    assert key(ip_address("192.0.2.1")) != key(ip_address("192.0.2.2"))
    assert SECRET not in repr(key)
    assert "kkkk" not in repr(key)


@pytest.mark.unit
@pytest.mark.parametrize("secret", ["", "short-secret", SECRET + "=", "!" * 43, "b" * 43])
def test_secret_validation_rejects_noncanonical_inputs_without_disclosure(secret: str) -> None:
    with pytest.raises(ValueError, match="IP-HMAC") as error:
        HmacClientIpKey(SecretStr(secret))
    if secret:
        assert secret not in str(error.value)


@pytest.mark.unit
def test_settings_do_not_require_secret_until_auth_protection_is_built() -> None:
    settings = Settings(_env_file=None, database_url="postgresql://local")
    assert settings.auth_ip_hmac_secret is None
    settings = Settings(
        _env_file=None, database_url="postgresql://local", auth_ip_hmac_secret=SECRET
    )
    assert settings.auth_ip_hmac_secret is not None
    assert SECRET not in repr(settings)
    with pytest.raises(ValidationError) as error:
        Settings(
            _env_file=None,
            database_url="postgresql://local",
            auth_ip_hmac_secret="do-not-leak-this",
        )
    assert "do-not-leak-this" not in str(error.value)


@pytest.mark.unit
def test_unverified_resolver_does_not_trust_peer_or_forwarding_headers() -> None:
    assert (
        UnresolvedClientIpResolver().resolve(
            peer_host="192.0.2.1",
            headers=((b"x-forwarded-for", b"198.51.100.1"), (b"forwarded", b"for=198.51.100.2")),
        )
        is None
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "origins,insecure",
    [
        (("http://localhost:8000",), False),
        (("http://app.example.net",), True),
        (("https://app.example.net",), True),
        (("http://localhost:8000", "https://app.example.net"), True),
        ((), True),
    ],
)
def test_settings_reject_unsafe_origin_cookie_combinations(
    origins: tuple[str, ...], insecure: bool
) -> None:
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            database_url="postgresql://local",
            auth_allowed_origins=origins,
            auth_allow_insecure_local_cookies=insecure,
        )


@pytest.mark.unit
def test_settings_default_empty_deny_and_explicit_loopback_http_exception() -> None:
    settings = Settings(_env_file=None, database_url="postgresql://local")
    assert settings.auth_allowed_origins == ()
    assert not settings.auth_allow_insecure_local_cookies
    settings = Settings(
        _env_file=None,
        database_url="postgresql://local",
        auth_allowed_origins=("http://[::1]:8000",),
        auth_allow_insecure_local_cookies=True,
    )
    assert settings.auth_allowed_origins == ("http://[::1]:8000",)


@pytest.mark.unit
def test_composition_requires_secret_and_provides_shared_protection_components() -> None:
    with pytest.raises(ValueError, match="IP-HMAC"):
        build_auth_protection(Settings(_env_file=None, database_url="postgresql://local"))
    components = build_auth_protection(
        Settings(
            _env_file=None,
            database_url="postgresql://local",
            auth_ip_hmac_secret=SECRET,
        )
    )
    assert isinstance(components.client_ips, UnresolvedClientIpResolver)
    assert components.cookies is not None
