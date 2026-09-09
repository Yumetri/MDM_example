"""Framework-independent authentication domain values."""

import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from email_validator import EmailNotValidError, validate_email


class AuthInvariantError(ValueError):
    """Raised when an authentication domain value violates its contract."""


class UserRole(StrEnum):
    """The single effective role held by a HUMAN user."""

    USER = "USER"
    ADMIN = "ADMIN"
    SUPER_ADMIN = "SUPER_ADMIN"


class UserStatus(StrEnum):
    """Whether a user may obtain new credentials."""

    ACTIVE = "ACTIVE"
    DISABLED = "DISABLED"


class SecurityEventType(StrEnum):
    """Permanent user-security state changes recorded in PostgreSQL."""

    INITIAL_SUPER_ADMIN_BOOTSTRAPPED = "INITIAL_SUPER_ADMIN_BOOTSTRAPPED"
    USER_ROLE_CHANGED = "USER_ROLE_CHANGED"
    USER_DISABLED = "USER_DISABLED"
    USER_ENABLED = "USER_ENABLED"
    PASSWORD_CHANGED = "PASSWORD_CHANGED"
    PASSWORD_RESET = "PASSWORD_RESET"


class SecurityEventInitiatorType(StrEnum):
    """The trusted boundary that initiated a permanent security event."""

    AUTHENTICATED_USER = "AUTHENTICATED_USER"
    RESET_TOKEN = "RESET_TOKEN"
    BOOTSTRAP_CLI = "BOOTSTRAP_CLI"


def normalize_email(value: str) -> str:
    """Return the one canonical form used by storage, lookup, and uniqueness."""
    candidate = unicodedata.normalize("NFC", value.strip())
    try:
        validated = validate_email(candidate, check_deliverability=False)
    except EmailNotValidError:
        raise AuthInvariantError("email format is invalid") from None

    local_part = unicodedata.normalize("NFC", validated.local_part).casefold()
    ascii_domain = validated.ascii_domain
    if ascii_domain is None:
        raise AuthInvariantError("email domain must have an IDNA representation")
    normalized = f"{local_part}@{ascii_domain.lower()}"
    if len(normalized) > 254:
        raise AuthInvariantError("email must not exceed 254 characters")
    return normalized


@dataclass(frozen=True, slots=True)
class EmailAddress:
    """A canonical email used for identity, uniqueness, and exact lookup."""

    value: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "value", normalize_email(self.value))


@dataclass(frozen=True, slots=True)
class DisplayName:
    """A normalized display-only user name."""

    value: str

    def __post_init__(self) -> None:
        normalized = unicodedata.normalize("NFC", self.value).strip()
        if not 1 <= len(normalized) <= 100:
            raise AuthInvariantError("name must contain between 1 and 100 code points")
        if any(unicodedata.category(character) == "Cc" for character in normalized):
            raise AuthInvariantError("name must not contain control characters")
        object.__setattr__(self, "value", normalized)


@dataclass(frozen=True, slots=True)
class PlainPassword:
    """An NFC-normalized password whose value is redacted from representations."""

    _value: str = field(repr=False)

    def __post_init__(self) -> None:
        normalized = unicodedata.normalize("NFC", self._value)
        if not 15 <= len(normalized) <= 128:
            raise AuthInvariantError("password must contain between 15 and 128 code points")
        object.__setattr__(self, "_value", normalized)

    def reveal(self) -> str:
        """Reveal the password only to a credential-processing boundary."""
        return self._value


@dataclass(frozen=True, slots=True)
class User:
    """An immutable snapshot of the current HUMAN user security state."""

    id: UUID
    email: EmailAddress
    name: DisplayName
    password_hash: str = field(repr=False)
    role: UserRole
    status: UserStatus
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        if not self.password_hash:
            raise AuthInvariantError("password hash must not be empty")
        if self.created_at.utcoffset() is None or self.updated_at.utcoffset() is None:
            raise AuthInvariantError("user timestamps must be timezone-aware")
        if self.updated_at < self.created_at:
            raise AuthInvariantError("updated_at must not precede created_at")


@dataclass(frozen=True, slots=True)
class UserSecurityEvent:
    """One validated permanent user-security audit record."""

    id: UUID
    event_type: SecurityEventType
    subject_user_id: UUID
    occurred_at: datetime
    initiator_type: SecurityEventInitiatorType
    initiator_user_id: UUID | None = None
    initiator_role: UserRole | None = None
    previous_role: UserRole | None = None
    new_role: UserRole | None = None
    previous_status: UserStatus | None = None
    new_status: UserStatus | None = None

    def __post_init__(self) -> None:
        if self.occurred_at.utcoffset() is None:
            raise AuthInvariantError("security event timestamp must be timezone-aware")
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


def validate_security_event_contract(
    *,
    event_type: SecurityEventType,
    initiator_type: SecurityEventInitiatorType,
    initiator_user_id: UUID | None,
    initiator_role: UserRole | None,
    previous_role: UserRole | None,
    new_role: UserRole | None,
    previous_status: UserStatus | None,
    new_status: UserStatus | None,
) -> None:
    """Validate the trusted initiator and typed transition shape of one event."""
    authenticated = initiator_type is SecurityEventInitiatorType.AUTHENTICATED_USER
    has_identity = initiator_user_id is not None and initiator_role is not None
    lacks_identity = initiator_user_id is None and initiator_role is None
    if (authenticated and not has_identity) or (not authenticated and not lacks_identity):
        raise AuthInvariantError("security event initiator fields are inconsistent")

    role_transition = (previous_role, new_role)
    status_transition = (previous_status, new_status)
    if event_type is SecurityEventType.INITIAL_SUPER_ADMIN_BOOTSTRAPPED:
        valid = (
            initiator_type is SecurityEventInitiatorType.BOOTSTRAP_CLI
            and role_transition == (None, UserRole.SUPER_ADMIN)
            and status_transition == (None, UserStatus.ACTIVE)
        )
    elif event_type is SecurityEventType.USER_ROLE_CHANGED:
        valid = (
            authenticated
            and previous_role is not None
            and new_role is not None
            and previous_role is not new_role
            and status_transition == (None, None)
        )
    elif event_type is SecurityEventType.USER_DISABLED:
        valid = (
            authenticated
            and role_transition == (None, None)
            and status_transition == (UserStatus.ACTIVE, UserStatus.DISABLED)
        )
    elif event_type is SecurityEventType.USER_ENABLED:
        valid = (
            authenticated
            and role_transition == (None, None)
            and status_transition == (UserStatus.DISABLED, UserStatus.ACTIVE)
        )
    elif event_type is SecurityEventType.PASSWORD_CHANGED:
        valid = (
            authenticated and role_transition == (None, None) and status_transition == (None, None)
        )
    else:
        valid = (
            event_type is SecurityEventType.PASSWORD_RESET
            and initiator_type is SecurityEventInitiatorType.RESET_TOKEN
            and role_transition == (None, None)
            and status_transition == (None, None)
        )
    if not valid:
        raise AuthInvariantError("security event transition fields are inconsistent")
