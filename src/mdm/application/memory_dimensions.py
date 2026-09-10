"""Use cases and persistence port for Memory Dimensions."""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from mdm.application.audit import HumanMutationAuditFactory
from mdm.application.auth import HumanPrincipal
from mdm.application.authorization import AuthorizationAction, AuthorizationPolicy
from mdm.domain.audit import (
    AuditInvariantError,
    DimensionOperation,
    MasterCodeOperation,
    MutationAuditMetadata,
)
from mdm.domain.dimensions import Dimension, DimensionCode, DimensionValidationError, MemoryValue


class MemoryDimensionNotFound(RuntimeError):
    """No active Memory Dimension exists for the requested ID."""


class MemoryDimensionCodeConflict(RuntimeError):
    """The normalized Memory code is already reserved."""


class MemoryDimensionValueConflict(RuntimeError):
    """An equivalent Memory capacity is already reserved."""


class MemoryDimensionMultipleConflicts(RuntimeError):
    """Both the Memory code and equivalent capacity are already reserved."""


class MemoryDimensionRepositoryUnavailable(RuntimeError):
    """Memory Dimension persistence is temporarily unavailable."""


@dataclass(frozen=True, slots=True)
class MemoryDimensionCursor:
    """Decoded keyset position for descending creation order."""

    created_at: datetime
    id: UUID

    def __post_init__(self) -> None:
        if self.created_at.utcoffset() is None:
            raise ValueError("Memory Dimension cursor timestamp must be timezone-aware")
        if not isinstance(self.id, UUID):
            raise ValueError("Memory Dimension cursor id must be a UUID")


@dataclass(frozen=True, slots=True)
class MemoryDimensionPage:
    """One bounded Memory page plus whether another page exists."""

    items: tuple[Dimension[MemoryValue], ...]
    has_more: bool


class MemoryRepository(Protocol):
    """Persistence boundary for Memory Dimensions."""

    async def create(
        self,
        code: DimensionCode,
        value: MemoryValue,
        audit: MutationAuditMetadata,
    ) -> Dimension[MemoryValue]: ...

    async def get_active(self, dimension_id: UUID) -> Dimension[MemoryValue]: ...

    async def list_active(
        self,
        *,
        after: MemoryDimensionCursor | None,
        limit: int,
    ) -> MemoryDimensionPage: ...

    async def update_value(
        self,
        dimension_id: UUID,
        expected_version: int,
        value: MemoryValue | None,
        audit: MutationAuditMetadata,
        *,
        code: DimensionCode | None = None,
    ) -> Dimension[MemoryValue]: ...


class CreateMemory:
    def __init__(
        self,
        repository: MemoryRepository,
        authorization: AuthorizationPolicy,
        audit_factory: HumanMutationAuditFactory,
    ) -> None:
        self._repository = repository
        self._authorization = authorization
        self._audit_factory = audit_factory

    async def execute(
        self,
        principal: HumanPrincipal,
        *,
        code: str,
        amount: int,
        unit: str,
        reason: str | None,
    ) -> Dimension[MemoryValue]:
        self._authorization.authorize(principal, AuthorizationAction.MUTATE_DATA)
        normalized_code = DimensionCode(code)
        normalized_value = MemoryValue.create(amount=amount, unit=unit)
        try:
            audit = self._audit_factory.create(
                principal,
                dimension_operation=DimensionOperation.CREATE,
                reason=reason,
            )
        except AuditInvariantError as error:
            raise DimensionValidationError(
                "reason", "유효한 변경 사유를 입력해야 합니다."
            ) from error
        return await self._repository.create(normalized_code, normalized_value, audit)


class UpdateMemoryValue:
    def __init__(
        self,
        repository: MemoryRepository,
        authorization: AuthorizationPolicy,
        audit_factory: HumanMutationAuditFactory,
    ) -> None:
        self._repository = repository
        self._authorization = authorization
        self._audit_factory = audit_factory

    async def execute(
        self,
        principal: HumanPrincipal,
        dimension_id: UUID,
        *,
        expected_version: int,
        amount: int | None = None,
        unit: str | None = None,
        code: str | None = None,
        reason: str | None = None,
    ) -> Dimension[MemoryValue]:
        self._authorization.authorize(principal, AuthorizationAction.MUTATE_DATA)
        if type(expected_version) is not int or expected_version < 1:
            raise ValueError("expected version must be a positive integer")
        if code is None and amount is None and unit is None:
            raise ValueError("code or value is required")
        if (amount is None) != (unit is None):
            raise ValueError("memory amount and unit must be provided together")
        normalized_code = None if code is None else DimensionCode(code)
        normalized_value = (
            None if amount is None or unit is None else MemoryValue.create(amount=amount, unit=unit)
        )
        try:
            audit = self._audit_factory.create(
                principal,
                dimension_operation=DimensionOperation.UPDATE,
                master_code_operation=(
                    MasterCodeOperation.RECOMPOSE if normalized_code is not None else None
                ),
                reason=reason,
            )
        except AuditInvariantError as error:
            raise DimensionValidationError(
                "reason", "유효한 변경 사유를 입력해야 합니다."
            ) from error
        return await self._repository.update_value(
            dimension_id,
            expected_version,
            normalized_value,
            audit,
            code=normalized_code,
        )


class GetMemory:
    def __init__(self, repository: MemoryRepository, authorization: AuthorizationPolicy) -> None:
        self._repository = repository
        self._authorization = authorization

    async def execute(
        self, principal: HumanPrincipal, dimension_id: UUID
    ) -> Dimension[MemoryValue]:
        self._authorization.authorize(principal, AuthorizationAction.READ_DATA)
        return await self._repository.get_active(dimension_id)


class ListMemories:
    def __init__(self, repository: MemoryRepository, authorization: AuthorizationPolicy) -> None:
        self._repository = repository
        self._authorization = authorization

    async def execute(
        self,
        principal: HumanPrincipal,
        *,
        after: MemoryDimensionCursor | None,
        limit: int,
    ) -> MemoryDimensionPage:
        self._authorization.authorize(principal, AuthorizationAction.READ_DATA)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Memory Dimension page limit must be between 1 and 100")
        return await self._repository.list_active(after=after, limit=limit)
