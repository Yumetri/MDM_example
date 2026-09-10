"""Use cases and persistence ports for Year and Network Dimensions."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from mdm.application.audit import HumanMutationAuditFactory
from mdm.application.auth import HumanPrincipal
from mdm.application.authorization import AuthorizationAction, AuthorizationPolicy
from mdm.domain.audit import AuditInvariantError, DimensionOperation, MutationAuditMetadata
from mdm.domain.dimensions import (
    Dimension,
    DimensionCode,
    DimensionValidationError,
    NetworkGeneration,
    YearValue,
)


class NumericDimensionNotFound(RuntimeError):
    """No active row exists for a requested numeric Dimension."""

    def __init__(self, dimension_name: str) -> None:
        super().__init__()
        self.dimension_name = dimension_name


class NumericDimensionCodeConflict(RuntimeError):
    """The normalized code is already reserved for one Dimension type."""

    def __init__(self, dimension_name: str) -> None:
        super().__init__()
        self.dimension_name = dimension_name


class NumericDimensionValueConflict(RuntimeError):
    """The numeric value is already reserved for one Dimension type."""

    def __init__(self, dimension_name: str) -> None:
        super().__init__()
        self.dimension_name = dimension_name


class NumericDimensionMultipleConflicts(RuntimeError):
    """Both fields are already reserved for one Dimension type."""

    def __init__(self, dimension_name: str) -> None:
        super().__init__()
        self.dimension_name = dimension_name


class NumericDimensionRepositoryUnavailable(RuntimeError):
    """Numeric Dimension persistence is temporarily unavailable."""


@dataclass(frozen=True, slots=True)
class NumericDimensionCursor:
    """Decoded keyset position for descending creation order."""

    created_at: datetime
    id: UUID

    def __post_init__(self) -> None:
        if self.created_at.utcoffset() is None:
            raise ValueError("numeric Dimension cursor timestamp must be timezone-aware")
        if not isinstance(self.id, UUID):
            raise ValueError("numeric Dimension cursor id must be a UUID")


@dataclass(frozen=True, slots=True)
class NumericDimensionPage[ValueT]:
    """One bounded page plus whether another keyset page exists."""

    items: tuple[Dimension[ValueT], ...]
    has_more: bool


class NumericDimensionRepository[ValueT](Protocol):
    """Persistence operations shared without erasing the concrete value type."""

    async def create(
        self,
        code: DimensionCode,
        value: ValueT,
        audit: MutationAuditMetadata,
    ) -> Dimension[ValueT]: ...

    async def get_active(self, dimension_id: UUID) -> Dimension[ValueT]: ...

    async def list_active(
        self,
        *,
        after: NumericDimensionCursor | None,
        limit: int,
    ) -> NumericDimensionPage[ValueT]: ...

    async def update_value(
        self,
        dimension_id: UUID,
        expected_version: int,
        value: ValueT,
        audit: MutationAuditMetadata,
    ) -> Dimension[ValueT]: ...


class YearRepository(NumericDimensionRepository[YearValue], Protocol):
    """Persistence boundary for Year Dimensions."""


class NetworkRepository(NumericDimensionRepository[NetworkGeneration], Protocol):
    """Persistence boundary for Network Dimensions."""


class _CreateNumericDimension[ValueT]:
    def __init__(
        self,
        repository: NumericDimensionRepository[ValueT],
        authorization: AuthorizationPolicy,
        audit_factory: HumanMutationAuditFactory,
        value_type: Callable[[int], ValueT],
    ) -> None:
        self._repository = repository
        self._authorization = authorization
        self._audit_factory = audit_factory
        self._value_type = value_type

    async def execute(
        self,
        principal: HumanPrincipal,
        *,
        code: str,
        value: int,
        reason: str | None,
    ) -> Dimension[ValueT]:
        self._authorization.authorize(principal, AuthorizationAction.MUTATE_DATA)
        normalized_code = DimensionCode(code)
        normalized_value = self._value_type(value)
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


class _UpdateNumericDimensionValue[ValueT]:
    def __init__(
        self,
        repository: NumericDimensionRepository[ValueT],
        authorization: AuthorizationPolicy,
        audit_factory: HumanMutationAuditFactory,
        value_type: Callable[[int], ValueT],
    ) -> None:
        self._repository = repository
        self._authorization = authorization
        self._audit_factory = audit_factory
        self._value_type = value_type

    async def execute(
        self,
        principal: HumanPrincipal,
        dimension_id: UUID,
        *,
        expected_version: int,
        value: int,
        reason: str | None,
    ) -> Dimension[ValueT]:
        self._authorization.authorize(principal, AuthorizationAction.MUTATE_DATA)
        if type(expected_version) is not int or expected_version < 1:
            raise ValueError("expected version must be a positive integer")
        normalized_value = self._value_type(value)
        try:
            audit = self._audit_factory.create(
                principal,
                dimension_operation=DimensionOperation.UPDATE,
                reason=reason,
            )
        except AuditInvariantError as error:
            raise DimensionValidationError(
                "reason", "유효한 변경 사유를 입력해야 합니다."
            ) from error
        return await self._repository.update_value(
            dimension_id, expected_version, normalized_value, audit
        )


class _GetNumericDimension[ValueT]:
    def __init__(
        self,
        repository: NumericDimensionRepository[ValueT],
        authorization: AuthorizationPolicy,
    ) -> None:
        self._repository = repository
        self._authorization = authorization

    async def execute(self, principal: HumanPrincipal, dimension_id: UUID) -> Dimension[ValueT]:
        self._authorization.authorize(principal, AuthorizationAction.READ_DATA)
        return await self._repository.get_active(dimension_id)


class _ListNumericDimensions[ValueT]:
    def __init__(
        self,
        repository: NumericDimensionRepository[ValueT],
        authorization: AuthorizationPolicy,
    ) -> None:
        self._repository = repository
        self._authorization = authorization

    async def execute(
        self,
        principal: HumanPrincipal,
        *,
        after: NumericDimensionCursor | None,
        limit: int,
    ) -> NumericDimensionPage[ValueT]:
        self._authorization.authorize(principal, AuthorizationAction.READ_DATA)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("numeric Dimension page limit must be between 1 and 100")
        return await self._repository.list_active(after=after, limit=limit)


class CreateYear(_CreateNumericDimension[YearValue]):
    def __init__(
        self,
        repository: YearRepository,
        authorization: AuthorizationPolicy,
        audit_factory: HumanMutationAuditFactory,
    ) -> None:
        super().__init__(repository, authorization, audit_factory, YearValue)


class UpdateYearValue(_UpdateNumericDimensionValue[YearValue]):
    def __init__(
        self,
        repository: YearRepository,
        authorization: AuthorizationPolicy,
        audit_factory: HumanMutationAuditFactory,
    ) -> None:
        super().__init__(repository, authorization, audit_factory, YearValue)


class GetYear(_GetNumericDimension[YearValue]):
    pass


class ListYears(_ListNumericDimensions[YearValue]):
    pass


class CreateNetwork(_CreateNumericDimension[NetworkGeneration]):
    def __init__(
        self,
        repository: NetworkRepository,
        authorization: AuthorizationPolicy,
        audit_factory: HumanMutationAuditFactory,
    ) -> None:
        super().__init__(repository, authorization, audit_factory, NetworkGeneration)


class UpdateNetworkValue(_UpdateNumericDimensionValue[NetworkGeneration]):
    def __init__(
        self,
        repository: NetworkRepository,
        authorization: AuthorizationPolicy,
        audit_factory: HumanMutationAuditFactory,
    ) -> None:
        super().__init__(repository, authorization, audit_factory, NetworkGeneration)


class GetNetwork(_GetNumericDimension[NetworkGeneration]):
    pass


class ListNetworks(_ListNumericDimensions[NetworkGeneration]):
    pass
