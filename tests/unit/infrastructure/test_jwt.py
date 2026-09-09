import json
from pathlib import Path
from uuid import UUID

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

from mdm.application.auth import InvalidAccessToken
from mdm.domain.auth import UserRole
from mdm.infrastructure.jwt import (
    AccessJwtCodec,
    JwtKeyConfigurationError,
    JwtKeyPair,
    build_access_jwt_codec,
)
from mdm.infrastructure.settings import Settings

USER_ID = UUID("018f3f0e-7b2a-7e8f-9f62-9876543210ab")
JTI = UUID("123e4567-e89b-42d3-a456-426614174000")
ISSUED_AT = 2_000_000_000
DATABASE_URL = "postgresql+asyncpg://mdm:secret@localhost:5432/mdm"


def _claims(**overrides: object) -> dict[str, object]:
    claims: dict[str, object] = {
        "iss": "mdm-api",
        "sub": str(USER_ID),
        "aud": "mdm-api",
        "roles": ["USER"],
        "iat": ISSUED_AT,
        "exp": ISSUED_AT + 900,
        "jti": str(JTI),
    }
    claims.update(overrides)
    return claims


def _write_key_files(
    directory: Path,
    *,
    kid: str,
    private_key: rsa.RSAPrivateKey,
    include_private_jwk_fields: bool = False,
) -> tuple[Path, Path]:
    private_key_path = directory / "jwt-private.pem"
    private_key_path.write_bytes(
        private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    public_jwk = json.loads(RSAAlgorithm.to_jwk(private_key.public_key()))
    public_jwk.update({"kid": kid, "alg": "RS256", "use": "sig"})
    if include_private_jwk_fields:
        public_jwk["d"] = "must-not-be-accepted"
    jwks_path = directory / "jwt-public-keys.json"
    jwks_path.write_text(json.dumps({"keys": [public_jwk]}), encoding="utf-8")
    return private_key_path, jwks_path


@pytest.fixture(scope="module")
def key_pair() -> JwtKeyPair:
    return JwtKeyPair.from_private_key("active-key", rsa.generate_private_key(65537, 2048))


@pytest.fixture
def codec(key_pair: JwtKeyPair) -> AccessJwtCodec:
    return AccessJwtCodec(
        active_key=key_pair,
        verification_keys={key_pair.kid: key_pair.public_key},
        jti_factory=lambda: JTI,
    )


@pytest.mark.unit
def test_access_jwt_contains_only_the_contract_claims_and_headers(
    codec: AccessJwtCodec,
) -> None:
    token = codec.issue(USER_ID, UserRole.ADMIN, issued_at=ISSUED_AT)

    header = jwt.get_unverified_header(token.reveal())
    payload = jwt.decode(token.reveal(), options={"verify_signature": False})

    assert header == {"alg": "RS256", "kid": "active-key", "typ": "JWT"}
    assert payload == {
        "iss": "mdm-api",
        "sub": str(USER_ID),
        "aud": "mdm-api",
        "roles": ["ADMIN"],
        "iat": ISSUED_AT,
        "exp": ISSUED_AT + 900,
        "jti": str(JTI),
    }
    assert "email" not in payload
    assert "name" not in payload
    assert "SYSTEM" not in token.reveal()
    assert "eyJ" not in repr(token)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("now", "accepted"),
    [
        (ISSUED_AT + 899, True),
        (ISSUED_AT + 900, True),
        (ISSUED_AT + 929, True),
        (ISSUED_AT + 930, False),
    ],
)
def test_access_jwt_expiry_boundary(
    codec: AccessJwtCodec,
    now: int,
    accepted: bool,
) -> None:
    token = codec.issue(USER_ID, UserRole.USER, issued_at=ISSUED_AT)

    if accepted:
        claims = codec.verify(token.reveal(), now=now)
        assert claims.user_id == USER_ID
        assert claims.role is UserRole.USER
    else:
        with pytest.raises(InvalidAccessToken):
            codec.verify(token.reveal(), now=now)


@pytest.mark.unit
@pytest.mark.parametrize(("future_seconds", "accepted"), [(30, True), (31, False)])
def test_access_jwt_future_iat_boundary(
    codec: AccessJwtCodec,
    future_seconds: int,
    accepted: bool,
) -> None:
    now = ISSUED_AT - future_seconds
    token = codec.issue(USER_ID, UserRole.USER, issued_at=ISSUED_AT)

    if accepted:
        codec.verify(token.reveal(), now=now)
    else:
        with pytest.raises(InvalidAccessToken):
            codec.verify(token.reveal(), now=now)


@pytest.mark.unit
def test_access_jwt_rejects_unregistered_kid(key_pair: JwtKeyPair) -> None:
    token = jwt.encode(
        _claims(),
        key_pair.private_key,
        algorithm="RS256",
        headers={"kid": "unknown-key", "typ": "JWT"},
    )
    codec = AccessJwtCodec(
        active_key=key_pair,
        verification_keys={key_pair.kid: key_pair.public_key},
    )

    with pytest.raises(InvalidAccessToken):
        codec.verify(token, now=ISSUED_AT)


@pytest.mark.unit
def test_access_jwt_rejects_extra_claims(codec: AccessJwtCodec, key_pair: JwtKeyPair) -> None:
    token = jwt.encode(
        _claims(email="secret@example.com"),
        key_pair.private_key,
        algorithm="RS256",
        headers={"kid": key_pair.kid, "typ": "JWT"},
    )

    with pytest.raises(InvalidAccessToken):
        codec.verify(token, now=ISSUED_AT)


@pytest.mark.unit
@pytest.mark.parametrize(
    "claims",
    [
        _claims(iss="other-service"),
        _claims(aud="other-service"),
        _claims(aud=["mdm-api", "other-api"]),
        _claims(roles=["USER", "ADMIN"]),
        _claims(iat=True),
        _claims(exp=ISSUED_AT + 901),
        _claims(jti=str(USER_ID)),
        {key: value for key, value in _claims().items() if key != "jti"},
    ],
)
def test_access_jwt_rejects_invalid_contract_claims(
    codec: AccessJwtCodec,
    key_pair: JwtKeyPair,
    claims: dict[str, object],
) -> None:
    token = jwt.encode(
        claims,
        key_pair.private_key,
        algorithm="RS256",
        headers={"kid": key_pair.kid, "typ": "JWT"},
    )

    with pytest.raises(InvalidAccessToken):
        codec.verify(token, now=ISSUED_AT)


@pytest.mark.unit
def test_access_jwt_rejects_signature_from_another_key(
    codec: AccessJwtCodec,
    key_pair: JwtKeyPair,
) -> None:
    attacker_key = rsa.generate_private_key(65537, 2048)
    token = jwt.encode(
        _claims(),
        attacker_key,
        algorithm="RS256",
        headers={"kid": key_pair.kid, "typ": "JWT"},
    )

    with pytest.raises(InvalidAccessToken):
        codec.verify(token, now=ISSUED_AT)


@pytest.mark.unit
def test_access_jwt_requires_rsa_key_of_at_least_2048_bits() -> None:
    with pytest.raises(ValueError, match="2048"):
        JwtKeyPair.from_private_key("weak", rsa.generate_private_key(65537, 1024))


@pytest.mark.unit
def test_access_jwt_requires_active_kid_in_verification_ring(key_pair: JwtKeyPair) -> None:
    with pytest.raises(ValueError, match="active kid"):
        AccessJwtCodec(active_key=key_pair, verification_keys={})


@pytest.mark.unit
def test_retired_public_key_can_verify_tokens_through_the_930_second_window(
    key_pair: JwtKeyPair,
) -> None:
    retired = JwtKeyPair.from_private_key(
        "retired-key",
        rsa.generate_private_key(65537, 2048),
    )
    old_codec = AccessJwtCodec(
        active_key=retired,
        verification_keys={retired.kid: retired.public_key},
        jti_factory=lambda: JTI,
    )
    token = old_codec.issue(USER_ID, UserRole.USER, issued_at=ISSUED_AT)
    rotated_codec = AccessJwtCodec(
        active_key=key_pair,
        verification_keys={
            key_pair.kid: key_pair.public_key,
            retired.kid: retired.public_key,
        },
    )

    claims = rotated_codec.verify(token.reveal(), now=ISSUED_AT + 929)

    assert claims.user_id == USER_ID


@pytest.mark.unit
def test_numeric_dates_remain_integers(codec: AccessJwtCodec) -> None:
    token = codec.issue(USER_ID, UserRole.USER, issued_at=ISSUED_AT)
    claims = codec.verify(token.reveal(), now=ISSUED_AT)

    assert isinstance(claims.issued_at, int)
    assert isinstance(claims.expires_at, int)
    assert claims.jti.version == 4


@pytest.mark.unit
def test_access_jwt_generates_a_fresh_uuid4_jti_inside_the_signer(
    key_pair: JwtKeyPair,
) -> None:
    codec = AccessJwtCodec(
        active_key=key_pair,
        verification_keys={key_pair.kid: key_pair.public_key},
    )

    first = jwt.decode(
        codec.issue(USER_ID, UserRole.USER, issued_at=ISSUED_AT).reveal(),
        options={"verify_signature": False},
    )
    second = jwt.decode(
        codec.issue(USER_ID, UserRole.USER, issued_at=ISSUED_AT).reveal(),
        options={"verify_signature": False},
    )

    assert UUID(first["jti"]).version == 4
    assert UUID(second["jti"]).version == 4
    assert first["jti"] != second["jti"]


@pytest.mark.unit
@pytest.mark.parametrize("issued_at", [True, 1.5])
def test_access_jwt_issuer_rejects_non_integer_numeric_date(
    codec: AccessJwtCodec,
    issued_at: object,
) -> None:
    with pytest.raises(ValueError, match="integer"):
        codec.issue(USER_ID, UserRole.USER, issued_at=issued_at)  # type: ignore[arg-type]


@pytest.mark.unit
def test_access_jwt_codec_loads_private_pem_and_public_jwks_from_typed_paths(
    tmp_path: Path,
) -> None:
    private_key = rsa.generate_private_key(65537, 2048)
    private_key_path, jwks_path = _write_key_files(
        tmp_path,
        kid="active-key",
        private_key=private_key,
    )
    settings = Settings(
        database_url=DATABASE_URL,
        auth_jwt_active_kid="active-key",
        auth_jwt_private_key_path=private_key_path,
        auth_jwt_jwks_path=jwks_path,
        _env_file=None,
    )

    codec = build_access_jwt_codec(settings, jti_factory=lambda: JTI)
    token = codec.issue(USER_ID, UserRole.USER, issued_at=ISSUED_AT)

    assert codec.verify(token.reveal(), now=ISSUED_AT).jti == JTI


@pytest.mark.unit
def test_access_jwt_codec_fails_closed_when_file_configuration_is_missing() -> None:
    settings = Settings(database_url=DATABASE_URL, _env_file=None)

    with pytest.raises(JwtKeyConfigurationError, match="required"):
        build_access_jwt_codec(settings)


@pytest.mark.unit
def test_access_jwt_codec_rejects_malformed_private_pem_without_exposing_it(
    tmp_path: Path,
) -> None:
    malformed_private_key = "private-secret-must-not-appear"
    private_key_path = tmp_path / "jwt-private.pem"
    private_key_path.write_text(malformed_private_key, encoding="utf-8")
    jwks_path = tmp_path / "jwt-public-keys.json"
    jwks_path.write_text('{"keys": []}', encoding="utf-8")
    settings = Settings(
        database_url=DATABASE_URL,
        auth_jwt_active_kid="active-key",
        auth_jwt_private_key_path=private_key_path,
        auth_jwt_jwks_path=jwks_path,
        _env_file=None,
    )

    with pytest.raises(JwtKeyConfigurationError) as raised:
        build_access_jwt_codec(settings)

    assert malformed_private_key not in str(raised.value)


@pytest.mark.unit
def test_access_jwt_codec_rejects_private_material_in_public_jwks(tmp_path: Path) -> None:
    private_key = rsa.generate_private_key(65537, 2048)
    private_key_path, jwks_path = _write_key_files(
        tmp_path,
        kid="active-key",
        private_key=private_key,
        include_private_jwk_fields=True,
    )
    settings = Settings(
        database_url=DATABASE_URL,
        auth_jwt_active_kid="active-key",
        auth_jwt_private_key_path=private_key_path,
        auth_jwt_jwks_path=jwks_path,
        _env_file=None,
    )

    with pytest.raises(JwtKeyConfigurationError, match="public"):
        build_access_jwt_codec(settings)


@pytest.mark.unit
def test_access_jwt_codec_rejects_private_and_public_key_mismatch(tmp_path: Path) -> None:
    private_key = rsa.generate_private_key(65537, 2048)
    other_private_key = rsa.generate_private_key(65537, 2048)
    private_key_path, _ = _write_key_files(
        tmp_path,
        kid="active-key",
        private_key=private_key,
    )
    _, jwks_path = _write_key_files(
        tmp_path,
        kid="active-key",
        private_key=other_private_key,
    )
    private_key_path.write_bytes(
        private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    settings = Settings(
        database_url=DATABASE_URL,
        auth_jwt_active_kid="active-key",
        auth_jwt_private_key_path=private_key_path,
        auth_jwt_jwks_path=jwks_path,
        _env_file=None,
    )

    with pytest.raises(JwtKeyConfigurationError, match="invalid"):
        build_access_jwt_codec(settings)
