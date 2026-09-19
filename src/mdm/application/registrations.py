"""Email registration use cases and transaction ports."""

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Protocol
from uuid import UUID

from mdm.application.auth import (
    AccessTokenSigner,
    OperationalEvent,
    OperationalEventSink,
    PasswordHasher,
)
from mdm.application.email_delivery import (
    EmailDeliveryMessage,
    EmailDeliveryStatus,
    EmailMessageType,
    EmailSender,
)
from mdm.application.sessions import RefreshDigestConflict, SessionGrant
from mdm.application.tokens import OpaqueTokenCollision
from mdm.application.users import DuplicateUserEmail, NewUser
from mdm.domain.auth import DisplayName, EmailAddress, PlainPassword, User, UserRole, UserStatus
from mdm.domain.credentials import RefreshToken, RegistrationToken, generate_opaque_token


class InvalidRegistrationToken(RuntimeError):
    """A registration credential cannot complete email ownership verification."""


class EmailDomainNotAllowed(RuntimeError):
    """The current public-registration policy does not permit the email domain."""


class RegistrationUnavailable(RuntimeError):
    """Registration failed without exposing infrastructure diagnostics."""


class RegistrationDigestConflict(RuntimeError):
    """The attempted digest insert was rolled back to its savepoint."""


@dataclass(frozen=True, slots=True)
class RegistrationChallenge:
    id: UUID
    email: EmailAddress = field(repr=False)
    status: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class RegistrationTarget:
    challenge_id: UUID
    token_id: UUID
    email: EmailAddress = field(repr=False)


@dataclass(frozen=True, slots=True)
class StoredRegistrationToken:
    id: UUID
    challenge_id: UUID
    digest: bytes = field(repr=False)
    used_at: datetime | None


class RegistrationTransaction(Protocol):
    async def user_exists(self) -> bool: ...

    async def lock_active_challenge(self) -> RegistrationChallenge | None: ...

    async def expire(self, challenge_id: UUID) -> None: ...

    async def create_challenge(
        self, now: datetime, expires_at: datetime
    ) -> RegistrationChallenge: ...

    async def add_registration_token(
        self, challenge_id: UUID, token: RegistrationToken, now: datetime
    ) -> None: ...

    async def lock_challenge(self, challenge_id: UUID) -> RegistrationChallenge | None: ...

    async def lock_token(
        self, challenge_id: UUID, token_id: UUID
    ) -> StoredRegistrationToken | None: ...

    async def now(self) -> datetime:
        """Read database time after acquiring the required locks."""
        ...

    async def create_user(self, values: NewUser) -> User: ...

    async def create_family(self, user: User, now: datetime, expires_at: datetime) -> UUID: ...

    async def add_refresh_token(self, family_id: UUID, token: RefreshToken, now: datetime) -> None:
        """Insert in a savepoint, raising RefreshDigestConflict on a digest collision."""
        ...

    async def complete(self, challenge_id: UUID, token_id: UUID, now: datetime) -> None: ...


class RegistrationRepository(Protocol):
    async def find_token(self, digest: bytes) -> RegistrationTarget | None:
        """Identify the credential without locks; close the connection before returning."""
        ...

    def transaction(
        self, email: EmailAddress
    ) -> AbstractAsyncContextManager[RegistrationTransaction]:
        """Open a registration transaction, commit on success and otherwise roll back."""
        ...


def require_allowed_domain(email: EmailAddress, allowed_domains: tuple[str, ...]) -> None:
    if email.value.rsplit("@", 1)[1] not in allowed_domains:
        raise EmailDomainNotAllowed


class CompleteRegistration:
    def __init__(
        self,
        repository: RegistrationRepository,
        hasher: PasswordHasher,
        signer: AccessTokenSigner,
        *,
        allowed_domains: tuple[str, ...],
        refresh_tokens: Callable[[], RefreshToken] = lambda: generate_opaque_token(RefreshToken),
    ) -> None:
        self._repository = repository
        self._hasher = hasher
        self._signer = signer
        self._allowed_domains = allowed_domains
        self._refresh_tokens = refresh_tokens

    async def execute(
        self, token: RegistrationToken, name: DisplayName, password: PlainPassword
    ) -> SessionGrant:
        target = await self._repository.find_token(token.digest())
        if target is None:
            raise InvalidRegistrationToken
        require_allowed_domain(target.email, self._allowed_domains)
        password_hash = await self._hasher.hash(password)
        async with self._repository.transaction(target.email) as tx:
            challenge = await tx.lock_challenge(target.challenge_id)
            stored = await tx.lock_token(target.challenge_id, target.token_id)
            now = await tx.now()
            if (
                challenge is None
                or challenge.id != target.challenge_id
                or challenge.email != target.email
                or challenge.status != "ACTIVE"
                or now >= challenge.expires_at
                or stored is None
                or stored.id != target.token_id
                or stored.challenge_id != challenge.id
                or stored.digest != token.digest()
                or stored.used_at is not None
            ):
                raise InvalidRegistrationToken
            require_allowed_domain(challenge.email, self._allowed_domains)
            try:
                user = await tx.create_user(
                    NewUser(challenge.email, name, password_hash, UserRole.USER, UserStatus.ACTIVE)
                )
            except DuplicateUserEmail:
                raise InvalidRegistrationToken from None
            expires_at = now + timedelta(days=7)
            family_id = await tx.create_family(user, now, expires_at)
            refresh = await self._add_refresh_token(tx, family_id, now)
            await tx.complete(challenge.id, stored.id, now)
            try:
                access = self._signer.issue(user.id, user.role, issued_at=int(now.timestamp()))
            except Exception:
                raise RegistrationUnavailable("registration signing failed") from None
            grant = SessionGrant(access, refresh, expires_at, now)
        return grant

    async def _add_refresh_token(
        self, tx: RegistrationTransaction, family_id: UUID, now: datetime
    ) -> RefreshToken:
        for _ in range(4):
            token = self._refresh_tokens()
            try:
                await tx.add_refresh_token(family_id, token, now)
            except RefreshDigestConflict:
                continue
            return token
        raise OpaqueTokenCollision("opaque token issuance failed")


class RequestRegistration:
    def __init__(
        self,
        repository: RegistrationRepository,
        sender: EmailSender,
        events: OperationalEventSink,
        *,
        allowed_domains: tuple[str, ...],
        clock: Callable[[], datetime],
        tokens: Callable[[], RegistrationToken] = lambda: generate_opaque_token(RegistrationToken),
    ) -> None:
        self._repository = repository
        self._sender = sender
        self._events = events
        self._allowed_domains = allowed_domains
        self._clock = clock
        self._tokens = tokens

    async def execute(self, email: EmailAddress) -> None:
        require_allowed_domain(email, self._allowed_domains)
        async with self._repository.transaction(email) as tx:
            if await tx.user_exists():
                return
            challenge = await tx.lock_active_challenge()
            now = await tx.now()
            if challenge is not None and now >= challenge.expires_at:
                await tx.expire(challenge.id)
                challenge = None
            if challenge is None:
                challenge = await tx.create_challenge(now, now + timedelta(days=1))
            token = await self._add_token(tx, challenge.id, now)
        result = await self._sender.send(
            EmailDeliveryMessage(EmailMessageType.REGISTRATION, email, token)
        )
        if result.status is not EmailDeliveryStatus.SENT:
            try:
                self._events.emit(
                    OperationalEvent(name="EMAIL_DELIVERY_FAILED", occurred_at=self._clock())
                )
            except Exception:
                pass

    async def _add_token(
        self, tx: RegistrationTransaction, challenge_id: UUID, now: datetime
    ) -> RegistrationToken:
        for _ in range(4):
            token = self._tokens()
            try:
                await tx.add_registration_token(challenge_id, token, now)
            except RegistrationDigestConflict:
                continue
            return token
        raise OpaqueTokenCollision("opaque token issuance failed")
