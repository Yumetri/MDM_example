"""Application ports and sanitized authentication failures."""

from dataclasses import dataclass, field
from typing import Protocol
from uuid import UUID

from mdm.domain.auth import PlainPassword, UserRole


class PasswordHashUnavailable(RuntimeError):
    """The bounded password-hashing capacity could not be acquired in time."""


class InvalidStoredPasswordHash(RuntimeError):
    """A stored password hash is malformed or unsupported."""


class InvalidAccessToken(RuntimeError):
    """An access token failed signature, claim, or time validation."""


@dataclass(frozen=True, slots=True, repr=False)
class EncodedAccessToken:
    """A signed JWT whose encoded credential is redacted from representations."""

    _value: str = field(repr=False)

    def reveal(self) -> str:
        """Return the encoded token only at a transport boundary."""
        return self._value

    def __repr__(self) -> str:
        return "EncodedAccessToken(value=<redacted>)"


@dataclass(frozen=True, slots=True)
class AccessTokenClaims:
    """Validated minimal claims consumed by the HUMAN principal boundary."""

    user_id: UUID
    role: UserRole
    issued_at: int
    expires_at: int
    jti: UUID


class PasswordHasher(Protocol):
    """Framework-independent password hashing boundary."""

    async def hash(self, password: PlainPassword) -> str:
        """Hash a normalized password without blocking the event loop."""
        ...

    async def verify(self, password: PlainPassword, encoded_hash: str) -> bool:
        """Verify a normalized password against a stored hash."""
        ...


class AccessTokenSigner(Protocol):
    """Issue the fixed minimal access-token profile."""

    def issue(
        self,
        user_id: UUID,
        role: UserRole,
        *,
        issued_at: int,
    ) -> EncodedAccessToken:
        """Sign one 900-second access token with a newly generated UUIDv4 jti."""
        ...


class AccessTokenVerifier(Protocol):
    """Validate an encoded access token without persistence access."""

    def verify(self, token: str, *, now: int) -> AccessTokenClaims:
        """Return trusted claims or raise InvalidAccessToken."""
        ...
