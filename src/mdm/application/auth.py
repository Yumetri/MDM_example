"""Application ports and sanitized authentication boundaries."""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Never, Protocol
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


@dataclass(frozen=True, slots=True)
class HumanPrincipal:
    """A trusted HUMAN identity derived only from a verified access token."""

    user_id: UUID
    role: UserRole


@dataclass(frozen=True, slots=True)
class OperationalEvent:
    """A minimal, sanitized event envelope safe for operational output."""

    name: Literal["INVALID_ACCESS_TOKEN"]
    occurred_at: datetime
    request_id: str | None = None

    def __post_init__(self) -> None:
        if self.occurred_at.utcoffset() is None:
            raise ValueError("operational event timestamp must be timezone-aware")
        if self.request_id is not None and (
            not self.request_id
            or len(self.request_id) > 128
            or not all(
                character.isascii() and character.isprintable() for character in self.request_id
            )
        ):
            raise ValueError("request_id must be a non-empty printable ASCII value")


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


class OperationalEventSink(Protocol):
    """Provider-neutral output boundary for sanitized operational events."""

    def emit(self, event: OperationalEvent) -> None:
        """Best-effort output of one event without credential material."""
        ...


class AuthenticateHumanPrincipal:
    """Convert one optional Bearer credential into a trusted HUMAN principal."""

    def __init__(
        self,
        verifier: AccessTokenVerifier,
        event_sink: OperationalEventSink,
        *,
        clock: Callable[[], datetime],
    ) -> None:
        self._verifier = verifier
        self._event_sink = event_sink
        self._clock = clock

    def execute(
        self,
        token: str | None,
        *,
        request_id: str | None = None,
    ) -> HumanPrincipal:
        """Authenticate without persistence and collapse every failure to one type."""
        occurred_at = self._clock()
        if occurred_at.utcoffset() is None:
            raise ValueError("authentication clock must return a timezone-aware datetime")
        if not token:
            self._reject(occurred_at=occurred_at, request_id=request_id)

        try:
            claims = self._verifier.verify(token, now=int(occurred_at.timestamp()))
        except InvalidAccessToken:
            self._reject(occurred_at=occurred_at, request_id=request_id)
        return HumanPrincipal(user_id=claims.user_id, role=claims.role)

    def _reject(self, *, occurred_at: datetime, request_id: str | None) -> Never:
        try:
            self._event_sink.emit(
                OperationalEvent(
                    name="INVALID_ACCESS_TOKEN",
                    occurred_at=occurred_at,
                    request_id=request_id,
                )
            )
        except Exception:
            pass
        raise InvalidAccessToken from None
