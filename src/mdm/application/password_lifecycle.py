"""Password lifecycle use cases with ordered, atomic credential mutations."""

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Protocol
from uuid import UUID

from mdm.application.auth import (
    HumanPrincipal,
    OperationalEvent,
    OperationalEventName,
    OperationalEventSink,
    PasswordHasher,
)
from mdm.application.email_delivery import (
    EmailDeliveryMessage,
    EmailDeliveryStatus,
    EmailMessageType,
    EmailSender,
)
from mdm.application.sessions import (
    InvalidSession,
    RefreshDigestConflict,
    RefreshFamily,
    RefreshTarget,
    SessionTransaction,
    StoredRefreshToken,
)
from mdm.application.tokens import OpaqueTokenCollision
from mdm.domain.auth import EmailAddress, PlainPassword, User, UserStatus
from mdm.domain.credentials import PasswordResetToken, RefreshToken, generate_opaque_token


class InvalidPasswordResetToken(RuntimeError):
    """Reset credentials are malformed, missing, used, expired or revoked."""


class InvalidCurrentPassword(RuntimeError):
    """The supplied current password does not authenticate this user."""


class PasswordLifecycleUnavailable(RuntimeError):
    """Password persistence failed without exposing infrastructure diagnostics."""


class ResetDigestConflict(RuntimeError):
    """A reset digest insert was rolled back to its savepoint."""


@dataclass(frozen=True, slots=True)
class ResetChallenge:
    id: UUID
    user_id: UUID
    status: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class ResetTarget:
    user_id: UUID
    challenge_id: UUID
    token_id: UUID


@dataclass(frozen=True, slots=True)
class StoredResetToken:
    id: UUID
    challenge_id: UUID
    digest: bytes = field(repr=False)
    used_at: datetime | None


@dataclass(frozen=True, slots=True)
class PasswordChangeGrant:
    refresh_token: RefreshToken = field(repr=False)
    expires_at: datetime
    issued_at: datetime


class PasswordTransaction(SessionTransaction, Protocol):
    async def lock_refresh_tokens(
        self, family_ids: tuple[UUID, ...]
    ) -> tuple[StoredRefreshToken, ...]: ...

    async def lock_challenges(
        self, challenge_id: UUID | None = None
    ) -> tuple[ResetChallenge, ...]: ...

    async def lock_reset_tokens(
        self, challenge_ids: tuple[UUID, ...]
    ) -> tuple[StoredResetToken, ...]: ...

    async def expire_challenge(self, challenge_id: UUID) -> None: ...

    async def create_challenge(self, now: datetime, expires_at: datetime) -> ResetChallenge: ...

    async def add_reset_token(
        self, challenge_id: UUID, token: PasswordResetToken, now: datetime
    ) -> None: ...

    async def change_hash(self, password_hash: str, now: datetime) -> None: ...

    async def complete_reset(self, challenge_id: UUID, token_id: UUID, now: datetime) -> None: ...

    async def revoke_challenge(self, challenge_id: UUID, now: datetime) -> None: ...

    async def audit_password(self, principal: HumanPrincipal | None, now: datetime) -> None: ...


class PasswordRepository(Protocol):
    async def get_by_email(self, email: EmailAddress) -> User | None: ...

    async def get_by_id(self, user_id: UUID) -> User | None: ...

    async def find_refresh(self, digest: bytes) -> RefreshTarget | None: ...

    async def find_reset(self, digest: bytes) -> ResetTarget | None: ...

    def transaction(self, user_id: UUID) -> AbstractAsyncContextManager[PasswordTransaction]:
        """Lock the user first; commit on success and roll back on failure."""
        ...


class PasswordLifecycle:
    def __init__(
        self,
        repository: PasswordRepository,
        hasher: PasswordHasher,
        events: OperationalEventSink,
        *,
        clock: Callable[[], datetime],
        refresh_tokens: Callable[[], RefreshToken] = lambda: generate_opaque_token(RefreshToken),
    ) -> None:
        self._repository = repository
        self._hasher = hasher
        self._events = events
        self._clock = clock
        self._refresh_tokens = refresh_tokens

    async def complete_reset(self, token: PasswordResetToken, password: PlainPassword) -> None:
        target = await self._repository.find_reset(token.digest())
        if target is None:
            raise InvalidPasswordResetToken
        snapshot = await self._repository.get_by_id(target.user_id)
        if snapshot is None or snapshot.status is not UserStatus.ACTIVE:
            raise InvalidPasswordResetToken
        password_hash = await self._hasher.hash(password)
        async with self._repository.transaction(target.user_id) as tx:
            user = tx.user
            if (
                user is None
                or user.id != target.user_id
                or user.status is not UserStatus.ACTIVE
                or user.password_hash != snapshot.password_hash
            ):
                raise InvalidPasswordResetToken
            families = await tx.lock_families()
            await tx.lock_refresh_tokens(tuple(f.id for f in families))
            challenges = await tx.lock_challenges(target.challenge_id)
            tokens = await tx.lock_reset_tokens(tuple(c.id for c in challenges))
            now = await tx.now()
            challenge = next((c for c in challenges if c.id == target.challenge_id), None)
            stored = next((t for t in tokens if t.id == target.token_id), None)
            if (
                challenge is None
                or challenge.user_id != user.id
                or challenge.status != "ACTIVE"
                or now >= challenge.expires_at
                or stored is None
                or stored.challenge_id != challenge.id
                or stored.digest != token.digest()
                or stored.used_at is not None
            ):
                raise InvalidPasswordResetToken
            await tx.change_hash(password_hash, now)
            for family in families:
                await tx.revoke(family.id, now)
            await tx.complete_reset(challenge.id, stored.id, now)
            await tx.audit_password(None, now)

    async def change_password(
        self,
        principal: HumanPrincipal,
        token: RefreshToken,
        current_password: PlainPassword,
        new_password: PlainPassword,
    ) -> PasswordChangeGrant:
        target = await self._repository.find_refresh(token.digest())
        if target is None:
            raise InvalidSession
        if target.user_id != principal.user_id:
            self._emit("SESSION_PRINCIPAL_MISMATCH")
            raise InvalidSession
        # Close this validation transaction before password verification or hashing.
        async with self._repository.transaction(target.user_id) as tx:
            snapshot = tx.user
            if (
                snapshot is None
                or snapshot.id != principal.user_id
                or snapshot.status is not UserStatus.ACTIVE
            ):
                raise InvalidSession
            families = await tx.lock_families()
            tokens = await tx.lock_refresh_tokens(tuple(f.id for f in families))
            self._require_refresh(target, token, families, tokens, await tx.now())
        if not await self._hasher.verify(current_password, snapshot.password_hash):
            raise InvalidCurrentPassword
        password_hash = await self._hasher.hash(new_password)
        async with self._repository.transaction(target.user_id) as tx:
            user = tx.user
            if (
                user is None
                or user.id != principal.user_id
                or user.status is not UserStatus.ACTIVE
                or user.password_hash != snapshot.password_hash
            ):
                raise InvalidSession
            families = await tx.lock_families()
            tokens = await tx.lock_refresh_tokens(tuple(f.id for f in families))
            challenges = await tx.lock_challenges()
            await tx.lock_reset_tokens(tuple(c.id for c in challenges))
            now = await tx.now()
            self._require_refresh(target, token, families, tokens, now)
            await tx.change_hash(password_hash, now)
            for family in families:
                await tx.revoke(family.id, now)
            expires_at = now + timedelta(days=7)
            family_id = await tx.create_family(now, expires_at)
            refresh = await self._add_refresh(tx, family_id, now)
            for challenge in challenges:
                await tx.revoke_challenge(challenge.id, now)
            await tx.audit_password(principal, now)
            grant = PasswordChangeGrant(refresh, expires_at, now)
        return grant

    @staticmethod
    def _require_refresh(
        target: RefreshTarget,
        token: RefreshToken,
        families: tuple[RefreshFamily, ...],
        tokens: tuple[StoredRefreshToken, ...],
        now: datetime,
    ) -> None:
        family = next((f for f in families if f.id == target.family_id), None)
        stored = next((t for t in tokens if t.id == target.token_id), None)
        if (
            family is None
            or family.user_id != target.user_id
            or family.status != "ACTIVE"
            or now >= family.expires_at
            or stored is None
            or stored.family_id != family.id
            or stored.digest != token.digest()
            or stored.used_at is not None
        ):
            raise InvalidSession

    async def _add_refresh(
        self, tx: PasswordTransaction, family_id: UUID, now: datetime
    ) -> RefreshToken:
        for _ in range(4):
            token = self._refresh_tokens()
            try:
                await tx.add_token(family_id, token, now)
            except RefreshDigestConflict:
                continue
            return token
        raise OpaqueTokenCollision("opaque token issuance failed")

    def _emit(self, name: OperationalEventName) -> None:
        try:
            self._events.emit(OperationalEvent(name=name, occurred_at=self._clock()))
        except Exception:
            pass


class RequestPasswordReset:
    def __init__(
        self,
        repository: PasswordRepository,
        sender: EmailSender,
        events: OperationalEventSink,
        *,
        clock: Callable[[], datetime],
        tokens: Callable[[], PasswordResetToken] = lambda: generate_opaque_token(
            PasswordResetToken
        ),
    ) -> None:
        self._repository = repository
        self._sender = sender
        self._events = events
        self._clock = clock
        self._tokens = tokens

    async def execute(self, email: EmailAddress) -> None:
        snapshot = await self._repository.get_by_email(email)
        if snapshot is None or snapshot.status is not UserStatus.ACTIVE:
            return
        async with self._repository.transaction(snapshot.id) as tx:
            user = tx.user
            if user is None or user.status is not UserStatus.ACTIVE or user.email != email:
                return
            challenges = await tx.lock_challenges()
            await tx.lock_reset_tokens(tuple(c.id for c in challenges))
            now = await tx.now()
            challenge = next((c for c in challenges if c.status == "ACTIVE"), None)
            if challenge is not None and now >= challenge.expires_at:
                await tx.expire_challenge(challenge.id)
                challenge = None
            if challenge is None:
                challenge = await tx.create_challenge(now, now + timedelta(minutes=30))
            token = await self._add_token(tx, challenge.id, now)
        result = await self._sender.send(
            EmailDeliveryMessage(EmailMessageType.PASSWORD_RESET, email, token)
        )
        if result.status is not EmailDeliveryStatus.SENT:
            try:
                self._events.emit(
                    OperationalEvent(name="EMAIL_DELIVERY_FAILED", occurred_at=self._clock())
                )
            except Exception:
                pass

    async def _add_token(
        self, tx: PasswordTransaction, challenge_id: UUID, now: datetime
    ) -> PasswordResetToken:
        for _ in range(4):
            token = self._tokens()
            try:
                await tx.add_reset_token(challenge_id, token, now)
            except ResetDigestConflict:
                continue
            return token
        raise OpaqueTokenCollision("opaque token issuance failed")
