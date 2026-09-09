"""Application data and repository ports for HUMAN users."""

from dataclasses import dataclass, field
from typing import Protocol
from uuid import UUID

from mdm.domain.auth import (
    DisplayName,
    EmailAddress,
    SecurityEventInitiatorType,
    SecurityEventType,
    User,
    UserRole,
    UserSecurityEvent,
    UserStatus,
    validate_security_event_contract,
)


class DuplicateUserEmail(RuntimeError):
    """A normalized email is already owned by another user."""


class UserPersistenceError(RuntimeError):
    """User persistence failed without exposing database diagnostics."""


@dataclass(frozen=True, slots=True)
class NewUser:
    """Values required to persist a new HUMAN user."""

    email: EmailAddress
    name: DisplayName
    password_hash: str = field(repr=False)
    role: UserRole
    status: UserStatus

    def __post_init__(self) -> None:
        if not self.password_hash:
            raise ValueError("password hash must not be empty")


@dataclass(frozen=True, slots=True)
class NewUserSecurityEvent:
    """Values for an insert-only permanent user-security event."""

    event_type: SecurityEventType
    subject_user_id: UUID
    initiator_type: SecurityEventInitiatorType
    initiator_user_id: UUID | None = None
    initiator_role: UserRole | None = None
    previous_role: UserRole | None = None
    new_role: UserRole | None = None
    previous_status: UserStatus | None = None
    new_status: UserStatus | None = None

    def __post_init__(self) -> None:
        validate_security_event_contract(
            event_type=self.event_type,
            initiator_type=self.initiator_type,
            initiator_user_id=self.initiator_user_id,
            initiator_role=self.initiator_role,
            previous_role=self.previous_role,
            new_role=self.new_role,
            previous_status=self.previous_status,
            new_status=self.new_status,
        )


class UserRepository(Protocol):
    """Persistence operations needed by user lifecycle use cases."""

    async def add(self, new_user: NewUser) -> User:
        """Insert a user or raise a sanitized persistence failure."""
        ...

    async def get_by_id(self, user_id: UUID) -> User | None:
        """Return the current user snapshot by stable ID."""
        ...

    async def get_by_email(self, email: EmailAddress) -> User | None:
        """Return the current user snapshot by normalized email."""
        ...


class UserSecurityEventRepository(Protocol):
    """Insert-only persistence boundary for permanent security events."""

    async def add(self, event: NewUserSecurityEvent) -> UserSecurityEvent:
        """Insert one event or raise a sanitized persistence failure."""
        ...
