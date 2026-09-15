"""Conditional Dimension lifecycle use cases and type-preserving persistence ports."""

from typing import Protocol
from uuid import UUID

from mdm.application.audit import HumanMutationAuditFactory
from mdm.application.auth import HumanPrincipal
from mdm.application.authorization import AuthorizationAction, AuthorizationPolicy
from mdm.domain.audit import AuditInvariantError, DimensionOperation, MutationAuditMetadata
from mdm.domain.dimensions import (
    BrandValue,
    CategoryValue,
    CompanyValue,
    CountryValue,
    Dimension,
    DimensionValidationError,
    MemoryValue,
    ModelValue,
    NetworkGeneration,
    YearValue,
)


class DimensionLifecycleNotFound(RuntimeError):
    """The requested row is absent or hidden from the requested operation."""


class DimensionNotDeleted(RuntimeError):
    """The requested tombstone or restoration target is active."""


class DimensionInUse(RuntimeError):
    """An active MasterCode still references the deletion target."""


class DimensionLifecycleUnavailable(RuntimeError):
    """Lifecycle persistence is temporarily unavailable."""


class DimensionLifecycleRepository[ValueT](Protocol):
    """Lifecycle operations retain the concrete Dimension value type."""

    async def get_tombstone(self, dimension_id: UUID) -> Dimension[ValueT]: ...

    async def delete(
        self,
        dimension_id: UUID,
        expected_version: int,
        audit: MutationAuditMetadata,
    ) -> Dimension[ValueT]: ...

    async def restore(
        self,
        dimension_id: UUID,
        expected_version: int,
        audit: MutationAuditMetadata,
    ) -> Dimension[ValueT]: ...


class GetDimensionTombstone[ValueT]:
    def __init__(
        self, repository: DimensionLifecycleRepository[ValueT], authorization: AuthorizationPolicy
    ) -> None:
        self._repository = repository
        self._authorization = authorization

    async def execute(self, principal: HumanPrincipal, dimension_id: UUID) -> Dimension[ValueT]:
        self._authorization.authorize(principal, AuthorizationAction.READ_TOMBSTONE)
        return await self._repository.get_tombstone(dimension_id)


class _MutateDimensionLifecycle[ValueT]:
    def __init__(
        self,
        repository: DimensionLifecycleRepository[ValueT],
        authorization: AuthorizationPolicy,
        audit_factory: HumanMutationAuditFactory,
        operation: DimensionOperation,
    ) -> None:
        self._repository = repository
        self._authorization = authorization
        self._audit_factory = audit_factory
        self._operation = operation

    async def execute(
        self,
        principal: HumanPrincipal,
        dimension_id: UUID,
        *,
        expected_version: int,
        reason: str | None,
    ) -> Dimension[ValueT]:
        self._authorization.authorize(principal, AuthorizationAction.MUTATE_DATA)
        if type(expected_version) is not int or expected_version < 1:
            raise ValueError("expected version must be a positive integer")
        try:
            audit = self._audit_factory.create(
                principal, dimension_operation=self._operation, reason=reason
            )
        except AuditInvariantError as error:
            raise DimensionValidationError(
                "reason", "유효한 변경 사유를 입력해야 합니다."
            ) from error
        if self._operation == DimensionOperation.DELETE:
            return await self._repository.delete(dimension_id, expected_version, audit)
        return await self._repository.restore(dimension_id, expected_version, audit)


class DeleteDimension[ValueT](_MutateDimensionLifecycle[ValueT]):
    def __init__(
        self,
        repository: DimensionLifecycleRepository[ValueT],
        authorization: AuthorizationPolicy,
        audit_factory: HumanMutationAuditFactory,
    ) -> None:
        super().__init__(repository, authorization, audit_factory, DimensionOperation.DELETE)


class RestoreDimension[ValueT](_MutateDimensionLifecycle[ValueT]):
    def __init__(
        self,
        repository: DimensionLifecycleRepository[ValueT],
        authorization: AuthorizationPolicy,
        audit_factory: HumanMutationAuditFactory,
    ) -> None:
        super().__init__(repository, authorization, audit_factory, DimensionOperation.RESTORE)


class CompanyLifecycleRepository(DimensionLifecycleRepository[CompanyValue], Protocol):
    """Persistence boundary for Company deletion, tombstone reads and restoration."""


class BrandLifecycleRepository(DimensionLifecycleRepository[BrandValue], Protocol):
    """Persistence boundary for Brand deletion, tombstone reads and restoration."""


class ModelLifecycleRepository(DimensionLifecycleRepository[ModelValue], Protocol):
    """Persistence boundary for Model deletion, tombstone reads and restoration."""


class CategoryLifecycleRepository(DimensionLifecycleRepository[CategoryValue], Protocol):
    """Persistence boundary for Category deletion, tombstone reads and restoration."""


class CountryLifecycleRepository(DimensionLifecycleRepository[CountryValue], Protocol):
    """Persistence boundary for Country deletion, tombstone reads and restoration."""


class YearLifecycleRepository(DimensionLifecycleRepository[YearValue], Protocol):
    """Persistence boundary for Year deletion, tombstone reads and restoration."""


class NetworkLifecycleRepository(DimensionLifecycleRepository[NetworkGeneration], Protocol):
    """Persistence boundary for Network deletion, tombstone reads and restoration."""


class MemoryLifecycleRepository(DimensionLifecycleRepository[MemoryValue], Protocol):
    """Persistence boundary for Memory deletion, tombstone reads and restoration."""
