"""RS256 access-token signing and verification."""

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm
from jwt.exceptions import InvalidKeyError

from mdm.application.auth import AccessTokenClaims, EncodedAccessToken, InvalidAccessToken
from mdm.domain.auth import UserRole
from mdm.infrastructure.settings import Settings

_ISSUER = "mdm-api"
_AUDIENCE = "mdm-api"
_LIFETIME_SECONDS = 900
_CLOCK_SKEW_SECONDS = 30
_CLAIM_NAMES = frozenset({"iss", "sub", "aud", "roles", "iat", "exp", "jti"})
_HEADER_NAMES = frozenset({"alg", "kid", "typ"})
_PRIVATE_JWK_PARAMETERS = frozenset({"d", "p", "q", "dp", "dq", "qi", "oth"})


class JwtKeyConfigurationError(RuntimeError):
    """JWT key files are missing, malformed, or mutually inconsistent."""


@dataclass(frozen=True, slots=True)
class JwtKeyPair:
    """An identified RSA signing key and its derived verification key."""

    kid: str
    private_key: rsa.RSAPrivateKey = field(repr=False)

    @classmethod
    def from_private_key(cls, kid: str, private_key: rsa.RSAPrivateKey) -> "JwtKeyPair":
        if not kid or not kid.isascii():
            raise ValueError("kid must be a non-empty ASCII identifier")
        if private_key.key_size < 2048:
            raise ValueError("RSA keys must contain at least 2048 bits")
        return cls(kid=kid, private_key=private_key)

    @property
    def public_key(self) -> rsa.RSAPublicKey:
        return self.private_key.public_key()


class AccessJwtCodec:
    """Issue and verify the fixed MDM RS256 access-token profile."""

    def __init__(
        self,
        *,
        active_key: JwtKeyPair,
        verification_keys: Mapping[str, rsa.RSAPublicKey],
        jti_factory: Callable[[], UUID] = uuid4,
    ) -> None:
        for public_key in verification_keys.values():
            if public_key.key_size < 2048:
                raise ValueError("RSA keys must contain at least 2048 bits")
        registered_active_key = verification_keys.get(active_key.kid)
        if registered_active_key is None:
            raise ValueError("active kid must be registered in the verification key ring")
        if registered_active_key.public_numbers() != active_key.public_key.public_numbers():
            raise ValueError("active kid must identify the active key pair")
        self._active_key = active_key
        self._verification_keys = dict(verification_keys)
        self._jti_factory = jti_factory

    def issue(
        self,
        user_id: UUID,
        role: UserRole,
        *,
        issued_at: int,
    ) -> EncodedAccessToken:
        if type(issued_at) is not int:
            raise ValueError("issued_at must be an integer NumericDate")
        jti = self._jti_factory()
        if jti.version != 4:
            raise ValueError("jti factory must return a UUIDv4 value")
        payload = {
            "iss": _ISSUER,
            "sub": str(user_id),
            "aud": _AUDIENCE,
            "roles": [role.value],
            "iat": issued_at,
            "exp": issued_at + _LIFETIME_SECONDS,
            "jti": str(jti),
        }
        encoded = jwt.encode(
            payload,
            self._active_key.private_key,
            algorithm="RS256",
            headers={"kid": self._active_key.kid, "typ": "JWT"},
        )
        return EncodedAccessToken(encoded)

    def verify(self, token: str, *, now: int) -> AccessTokenClaims:
        try:
            header = jwt.get_unverified_header(token)
            if set(header) != _HEADER_NAMES:
                raise InvalidAccessToken
            if header.get("alg") != "RS256" or header.get("typ") != "JWT":
                raise InvalidAccessToken
            kid = header.get("kid")
            if not isinstance(kid, str) or kid not in self._verification_keys:
                raise InvalidAccessToken
            payload: dict[str, Any] = jwt.decode(
                token,
                self._verification_keys[kid],
                algorithms=["RS256"],
                audience=_AUDIENCE,
                issuer=_ISSUER,
                options={
                    "require": sorted(_CLAIM_NAMES),
                    "verify_exp": False,
                    "verify_iat": False,
                    "verify_nbf": False,
                },
            )
            return self._validate_claims(payload, now=now)
        except InvalidAccessToken:
            raise
        except (jwt.PyJWTError, KeyError, TypeError, ValueError):
            raise InvalidAccessToken from None

    @staticmethod
    def _validate_claims(payload: Mapping[str, Any], *, now: int) -> AccessTokenClaims:
        if set(payload) != _CLAIM_NAMES:
            raise InvalidAccessToken
        if payload["iss"] != _ISSUER or payload["aud"] != _AUDIENCE:
            raise InvalidAccessToken
        roles = payload["roles"]
        issued_at = payload["iat"]
        expires_at = payload["exp"]
        if not isinstance(roles, list) or len(roles) != 1 or not isinstance(roles[0], str):
            raise InvalidAccessToken
        if type(issued_at) is not int or type(expires_at) is not int:
            raise InvalidAccessToken
        if expires_at != issued_at + _LIFETIME_SECONDS:
            raise InvalidAccessToken
        if now >= expires_at + _CLOCK_SKEW_SECONDS:
            raise InvalidAccessToken
        if issued_at > now + _CLOCK_SKEW_SECONDS:
            raise InvalidAccessToken
        try:
            user_id = UUID(payload["sub"])
            jti = UUID(payload["jti"])
            role = UserRole(roles[0])
        except (ValueError, AttributeError, TypeError):
            raise InvalidAccessToken from None
        if str(user_id) != payload["sub"] or str(jti) != payload["jti"] or jti.version != 4:
            raise InvalidAccessToken
        return AccessTokenClaims(
            user_id=user_id,
            role=role,
            issued_at=issued_at,
            expires_at=expires_at,
            jti=jti,
        )


def build_access_jwt_codec(
    settings: Settings,
    *,
    jti_factory: Callable[[], UUID] = uuid4,
) -> AccessJwtCodec:
    """Load a local private PEM and public JWKS through typed file settings."""
    active_kid = settings.auth_jwt_active_kid
    private_key_path = settings.auth_jwt_private_key_path
    jwks_path = settings.auth_jwt_jwks_path
    if active_kid is None or private_key_path is None or jwks_path is None:
        raise JwtKeyConfigurationError("JWT file settings are required")

    private_key = _load_private_key(private_key_path)
    verification_keys = _load_verification_keys(jwks_path)
    try:
        active_key = JwtKeyPair.from_private_key(active_kid, private_key)
        return AccessJwtCodec(
            active_key=active_key,
            verification_keys=verification_keys,
            jti_factory=jti_factory,
        )
    except ValueError:
        raise JwtKeyConfigurationError("JWT key configuration is invalid") from None


def _load_private_key(path: Path) -> rsa.RSAPrivateKey:
    try:
        encoded = path.read_bytes()
        private_key = serialization.load_pem_private_key(encoded, password=None)
    except (OSError, TypeError, ValueError):
        raise JwtKeyConfigurationError("JWT private key file is invalid") from None
    if not isinstance(private_key, rsa.RSAPrivateKey):
        raise JwtKeyConfigurationError("JWT private key file is invalid")
    return private_key


def _load_verification_keys(path: Path) -> dict[str, rsa.RSAPublicKey]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise JwtKeyConfigurationError("JWT public keyring file is invalid") from None
    if not isinstance(document, dict) or not isinstance(document.get("keys"), list):
        raise JwtKeyConfigurationError("JWT public keyring file is invalid")

    verification_keys: dict[str, rsa.RSAPublicKey] = {}
    for jwk in document["keys"]:
        if not isinstance(jwk, dict):
            raise JwtKeyConfigurationError("JWT public keyring file is invalid")
        kid = jwk.get("kid")
        if (
            not isinstance(kid, str)
            or not kid
            or not kid.isascii()
            or kid in verification_keys
            or jwk.get("kty") != "RSA"
            or jwk.get("alg") != "RS256"
            or jwk.get("use") != "sig"
            or _PRIVATE_JWK_PARAMETERS.intersection(jwk)
        ):
            raise JwtKeyConfigurationError("JWT public keyring file is invalid")
        try:
            public_key = RSAAlgorithm.from_jwk(jwk)
        except (InvalidKeyError, TypeError, ValueError):
            raise JwtKeyConfigurationError("JWT public keyring file is invalid") from None
        if not isinstance(public_key, rsa.RSAPublicKey):
            raise JwtKeyConfigurationError("JWT public keyring file is invalid")
        verification_keys[kid] = public_key
    return verification_keys
