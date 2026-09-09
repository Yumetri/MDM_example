"""Use case and persistence port for the initial SUPER_ADMIN bootstrap."""

from enum import StrEnum
from typing import Protocol

from mdm.application.auth import InvalidStoredPasswordHash, PasswordHasher
from mdm.application.users import DuplicateUserEmail, NewUser
from mdm.domain.auth import (
    DisplayName,
    EmailAddress,
    PlainPassword,
    User,
    UserRole,
    UserStatus,
)


class BootstrapStateConflict(RuntimeError):
    """The fixed bootstrap identity does not match the existing security state."""


class BootstrapPersistenceError(RuntimeError):
    """Bootstrap persistence did not converge without exposing diagnostics."""


class BootstrapOutcome(StrEnum):
    """Stable outcomes used by the CLI boundary."""

    CREATED = "CREATED"
    NO_OP = "NO_OP"


class BootstrapSuperAdminRepository(Protocol):
    """Transaction-owning persistence operations needed by bootstrap."""

    async def get_by_email(self, email: EmailAddress) -> User | None:
        """Read one user in a short-lived transaction."""
        ...

    async def create_with_security_event(self, new_user: NewUser) -> User:
        """Atomically create the user and its bootstrap security event."""
        ...


class BootstrapSuperAdmin:
    """Create or verify the one operator-selected bootstrap identity."""

    def __init__(
        self,
        repository: BootstrapSuperAdminRepository,
        password_hasher: PasswordHasher,
    ) -> None:
        self._repository = repository
        self._password_hasher = password_hasher

    async def execute(self, *, email: str, name: str, password: str) -> BootstrapOutcome:
        normalized_email = EmailAddress(email)
        display_name = DisplayName(name)
        plain_password = PlainPassword(password)

        existing = await self._repository.get_by_email(normalized_email)
        if existing is None:
            password_hash = await self._password_hasher.hash(plain_password)
            new_user = NewUser(
                email=normalized_email,
                name=display_name,
                password_hash=password_hash,
                role=UserRole.SUPER_ADMIN,
                status=UserStatus.ACTIVE,
            )
            try:
                await self._repository.create_with_security_event(new_user)
            except DuplicateUserEmail:
                existing = await self._repository.get_by_email(normalized_email)
                if existing is None:
                    raise BootstrapPersistenceError("BOOTSTRAP_PERSISTENCE_ERROR") from None
            else:
                return BootstrapOutcome.CREATED

        try:
            password_matches = await self._password_hasher.verify(
                plain_password,
                existing.password_hash,
            )
        except InvalidStoredPasswordHash:
            raise BootstrapStateConflict("BOOTSTRAP_STATE_CONFLICT") from None
        current = await self._repository.get_by_email(normalized_email)
        if (
            current is None
            or current.id != existing.id
            or current.password_hash != existing.password_hash
            or current.role is not existing.role
            or current.status is not existing.status
            or not password_matches
            or current.role is not UserRole.SUPER_ADMIN
            or current.status is not UserStatus.ACTIVE
        ):
            raise BootstrapStateConflict("BOOTSTRAP_STATE_CONFLICT")
        return BootstrapOutcome.NO_OP
