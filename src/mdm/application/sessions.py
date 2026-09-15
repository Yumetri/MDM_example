"""Credential use cases and transaction ports for PostgreSQL-backed sessions."""

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Protocol
from uuid import UUID

from mdm.application.auth import (
    AccessTokenSigner,
    EncodedAccessToken,
    HumanPrincipal,
    InvalidAccessToken,
    OperationalEvent,
    OperationalEventSink,
    PasswordHasher,
)
from mdm.application.tokens import OpaqueTokenCollision
from mdm.domain.auth import EmailAddress, PlainPassword, User, UserRole, UserStatus
from mdm.domain.credentials import RefreshToken, generate_opaque_token


class InvalidCredentials(RuntimeError):
    """Login failed without exposing account existence or status."""


class InvalidSession(RuntimeError):
    """The submitted refresh credential cannot authenticate a session."""


class RefreshConflict(RuntimeError):
    """A recently rotated credential was submitted again within the grace period."""


class SessionUnavailable(RuntimeError):
    """Session persistence failed without exposing database diagnostics."""


class RefreshDigestConflict(RuntimeError):
    """A token insert collided with a persisted digest; its savepoint was rolled back."""


@dataclass(frozen=True, slots=True)
class RefreshFamily:
    id: UUID
    user_id: UUID
    status: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class RefreshTarget:
    user_id: UUID
    family_id: UUID
    token_id: UUID


@dataclass(frozen=True, slots=True)
class StoredRefreshToken:
    id: UUID
    family_id: UUID
    digest: bytes = field(repr=False)
    used_at: datetime | None


@dataclass(frozen=True, slots=True)
class CurrentProfile:
    id: UUID
    email: str
    name: str
    effective_role: UserRole


@dataclass(frozen=True, slots=True)
class SessionGrant:
    access_token: EncodedAccessToken = field(repr=False)
    refresh_token: RefreshToken = field(repr=False)
    expires_at: datetime
    issued_at: datetime


class SessionTransaction(Protocol):
    """A transaction with its user row already locked and freshly loaded."""

    @property
    def user(self) -> User | None: ...

    async def lock_families(self, family_id: UUID | None = None) -> tuple[RefreshFamily, ...]:
        """Lock this user's families by UUID, before any token locks."""
        ...

    async def lock_tokens(self, family_id: UUID, token_id: UUID) -> tuple[StoredRefreshToken, ...]:
        """Lock a family's tokens in UUID order."""
        ...

    async def mark_used(
        self, token_id: UUID, replacement_id: UUID, family_id: UUID, now: datetime
    ) -> None: ...

    async def now(self) -> datetime:
        """Return current database time after acquiring the required locks."""
        ...

    async def revoke(self, family_id: UUID, now: datetime) -> None: ...

    async def create_family(self, now: datetime, expires_at: datetime) -> UUID: ...

    async def add_token(self, family_id: UUID, token: RefreshToken, now: datetime) -> UUID:
        """Insert within a savepoint; expose only a typed digest collision on conflict."""
        ...


class SessionRepository(Protocol):
    async def get_by_id(self, user_id: UUID) -> User | None: ...

    async def find_refresh(self, digest: bytes) -> RefreshTarget | None:
        """Identify a target without locks, closing the read session before returning."""
        ...

    async def get_by_email(self, email: EmailAddress) -> User | None:
        """Return a snapshot after closing the read session and connection."""
        ...

    def transaction(self, user_id: UUID) -> AbstractAsyncContextManager[SessionTransaction]:
        """Begin a transaction and lock its user before yielding; commit on success."""
        ...


class SessionUseCases:
    def __init__(
        self,
        repository: SessionRepository,
        password_hasher: PasswordHasher,
        signer: AccessTokenSigner,
        event_sink: OperationalEventSink,
        *,
        fake_password_hash: str,
        clock: Callable[[], datetime],
        refresh_tokens: Callable[[], RefreshToken] = lambda: generate_opaque_token(RefreshToken),
    ) -> None:
        self._repository = repository
        self._password_hasher = password_hasher
        self._signer = signer
        self._events = event_sink
        self._fake_password_hash = fake_password_hash
        self._clock = clock
        self._refresh_tokens = refresh_tokens

    async def login(self, email: EmailAddress, password: PlainPassword) -> SessionGrant:
        try:
            return await self._login(email, password)
        except InvalidCredentials:
            self._emit_login_failed()
            raise

    async def _login(self, email: EmailAddress, password: PlainPassword) -> SessionGrant:
        snapshot = await self._repository.get_by_email(email)
        password_matches = await self._password_hasher.verify(
            password,
            self._fake_password_hash if snapshot is None else snapshot.password_hash,
        )
        if snapshot is None or not password_matches or snapshot.status is not UserStatus.ACTIVE:
            raise InvalidCredentials
        async with self._repository.transaction(snapshot.id) as tx:
            current = tx.user
            if (
                current is None
                or current.status is not UserStatus.ACTIVE
                or current.password_hash != snapshot.password_hash
            ):
                raise InvalidCredentials
            families = await tx.lock_families()
            now = await tx.now()
            for family in families:
                if family.status == "ACTIVE":
                    await tx.revoke(family.id, now)
            expires_at = now + timedelta(days=7)
            family_id = await tx.create_family(now, expires_at)
            refresh, _ = await self._add_token(tx, family_id, now)
            access = self._signer.issue(current.id, current.role, issued_at=int(now.timestamp()))
            grant = SessionGrant(access, refresh, expires_at, now)
        return grant

    async def _add_token(
        self, tx: SessionTransaction, family_id: UUID, now: datetime
    ) -> tuple[RefreshToken, UUID]:
        for _ in range(4):
            token = self._refresh_tokens()
            try:
                token_id = await tx.add_token(family_id, token, now)
            except RefreshDigestConflict:
                continue
            return token, token_id
        raise OpaqueTokenCollision("opaque token issuance failed")

    def _emit_login_failed(self) -> None:
        try:
            self._events.emit(OperationalEvent(name="LOGIN_FAILED", occurred_at=self._clock()))
        except Exception:
            pass

    async def refresh(self, token: RefreshToken) -> SessionGrant:
        try:
            return await self._refresh(token)
        except RefreshConflict:
            try:
                self._events.emit(
                    OperationalEvent(name="REFRESH_CONFLICT", occurred_at=self._clock())
                )
            except Exception:
                pass
            raise

    async def _refresh(self, token: RefreshToken) -> SessionGrant:
        target = await self._repository.find_refresh(token.digest())
        if target is None:
            raise InvalidSession
        grant = None
        async with self._repository.transaction(target.user_id) as tx:
            current = tx.user
            if current is None or current.status is not UserStatus.ACTIVE:
                raise InvalidSession
            family, stored = await self._locked_refresh(tx, target, token)
            now = await tx.now()
            if family.status != "ACTIVE" or now >= family.expires_at:
                raise InvalidSession
            if stored.used_at is not None:
                if now <= stored.used_at + timedelta(seconds=10):
                    raise RefreshConflict
                await tx.revoke(family.id, now)
            else:
                refresh, replacement_id = await self._add_token(tx, family.id, now)
                await tx.mark_used(stored.id, replacement_id, family.id, now)
                access = self._signer.issue(
                    current.id, current.role, issued_at=int(now.timestamp())
                )
                grant = SessionGrant(access, refresh, family.expires_at, now)
        # A detected reuse must commit revocation before returning the rejection.
        if grant is None:
            raise InvalidSession
        return grant

    async def logout(self, token: RefreshToken) -> None:
        target = await self._repository.find_refresh(token.digest())
        if target is None:
            return
        async with self._repository.transaction(target.user_id) as tx:
            if tx.user is None:
                return
            try:
                family, stored = await self._locked_refresh(tx, target, token)
            except InvalidSession:
                return
            now = await tx.now()
            if family.status == "ACTIVE" and now < family.expires_at and stored.used_at is None:
                await tx.revoke(family.id, now)

    async def _locked_refresh(
        self, tx: SessionTransaction, target: RefreshTarget, token: RefreshToken
    ) -> tuple[RefreshFamily, StoredRefreshToken]:
        families = await tx.lock_families(target.family_id)
        family = next((row for row in families if row.id == target.family_id), None)
        if family is None or family.user_id != target.user_id:
            raise InvalidSession
        tokens = await tx.lock_tokens(family.id, target.token_id)
        stored = next((row for row in tokens if row.id == target.token_id), None)
        if stored is None or stored.family_id != family.id or stored.digest != token.digest():
            raise InvalidSession
        return family, stored


class GetCurrentProfile:
    def __init__(self, repository: SessionRepository) -> None:
        self._repository = repository

    async def execute(self, principal: HumanPrincipal) -> CurrentProfile:
        user = await self._repository.get_by_id(principal.user_id)
        if user is None:
            raise InvalidAccessToken
        return CurrentProfile(user.id, user.email.value, user.name.value, principal.role)
