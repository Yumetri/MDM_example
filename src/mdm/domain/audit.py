"""Framework-independent audit actor and mutation metadata values."""

import unicodedata
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from mdm.domain.auth import UserRole


class AuditInvariantError(ValueError):
    """Raised when trusted audit metadata violates the domain contract."""


class ActorKind(StrEnum):
    """The closed actor representation stored in domain audit logs."""

    HUMAN = "HUMAN"
    SYSTEM = "SYSTEM"


class DimensionOperation(StrEnum):
    """Dimension mutations understood by future audit triggers."""

    CREATE = "CREATE"
    UPDATE = "UPDATE"
    DELETE = "DELETE"
    RESTORE = "RESTORE"


class MasterCodeOperation(StrEnum):
    """MasterCode mutations understood by future audit triggers."""

    CREATE = "CREATE"
    REFERENCE_UPDATE = "REFERENCE_UPDATE"
    RECOMPOSE = "RECOMPOSE"
    DELETE = "DELETE"
    RESTORE = "RESTORE"


@dataclass(frozen=True, slots=True)
class AuditActor:
    """One validated actor snapshot shared by all logs in a mutation."""

    kind: ActorKind
    actor_id: str
    role: UserRole | None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, ActorKind):
            raise AuditInvariantError("actor kind is unsupported")
        if not isinstance(self.actor_id, str) or not 1 <= len(self.actor_id) <= 255:
            raise AuditInvariantError("actor id must contain between 1 and 255 characters")
        if self.actor_id.strip(" ") != self.actor_id:
            raise AuditInvariantError("actor id must not contain edge spaces")

        if self.kind is ActorKind.HUMAN:
            if not isinstance(self.role, UserRole):
                raise AuditInvariantError("human actor role is unsupported")
            try:
                user_id = UUID(self.actor_id)
            except (ValueError, AttributeError):
                raise AuditInvariantError("human actor id must be a UUID") from None
            if str(user_id) != self.actor_id:
                raise AuditInvariantError("human actor id must be a canonical UUID")
        elif self.role is not None:
            raise AuditInvariantError("system actor must not have a human role")

    @classmethod
    def human(cls, *, user_id: UUID, role: UserRole) -> "AuditActor":
        """Build the only actor shape connected to the current runtime."""
        if not isinstance(user_id, UUID):
            raise AuditInvariantError("human actor id must be a UUID")
        return cls(kind=ActorKind.HUMAN, actor_id=str(user_id), role=role)


@dataclass(frozen=True, slots=True)
class MutationOperations:
    """Entity-specific operations that share one mutation context."""

    dimension: DimensionOperation | None = None
    master_code: MasterCodeOperation | None = None

    def __post_init__(self) -> None:
        if self.dimension is not None and not isinstance(self.dimension, DimensionOperation):
            raise AuditInvariantError("dimension operation is unsupported")
        if self.master_code is not None and not isinstance(self.master_code, MasterCodeOperation):
            raise AuditInvariantError("master code operation is unsupported")
        if self.dimension is None and self.master_code is None:
            raise AuditInvariantError("at least one mutation operation is required")


def normalize_reason(value: str | None) -> str | None:
    """Normalize optional audit reason metadata without rewriting its content."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise AuditInvariantError("reason must be a string")
    normalized = value.strip(" ")
    if not normalized:
        return None
    if len(normalized) > 500:
        raise AuditInvariantError("reason must not exceed 500 characters")
    if any(unicodedata.category(character) == "Cc" for character in normalized):
        raise AuditInvariantError("reason must not contain control characters")
    return normalized


@dataclass(frozen=True, slots=True)
class MutationAuditMetadata:
    """Trusted application metadata awaiting a database mutation timestamp."""

    change_set_id: UUID
    actor: AuditActor
    operations: MutationOperations
    reason: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.change_set_id, UUID) or self.change_set_id.version != 7:
            raise AuditInvariantError("change set id must be a UUIDv7")
        if not isinstance(self.actor, AuditActor):
            raise AuditInvariantError("audit actor is invalid")
        if not isinstance(self.operations, MutationOperations):
            raise AuditInvariantError("mutation operations are invalid")
        object.__setattr__(self, "reason", normalize_reason(self.reason))
