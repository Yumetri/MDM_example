"""Use cases and persistence ports for the non-Company string Dimensions."""

from collections.abc import Callable
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
from mdm.domain.dimensions import (
    BrandValue,
    CategoryValue,
    CountryValue,
    Dimension,
    DimensionCode,
    DimensionValidationError,
    ModelValue,
)


class StringDimensionNotFound(RuntimeError):
    """No active row exists for a requested string Dimension."""

    def __init__(self, dimension_name: str) -> None:
        super().__init__()
        self.dimension_name = dimension_name


class StringDimensionCodeConflict(RuntimeError):
    """The normalized code is already reserved for one Dimension type."""

    def __init__(self, dimension_name: str) -> None:
        super().__init__()
        self.dimension_name = dimension_name


class StringDimensionValueConflict(RuntimeError):
    """The normalized value is already reserved for one Dimension type."""

    def __init__(self, dimension_name: str) -> None:
        super().__init__()
        self.dimension_name = dimension_name


class StringDimensionMultipleConflicts(RuntimeError):
    """Both normalized fields are already reserved for one Dimension type."""

    def __init__(self, dimension_name: str) -> None:
        super().__init__()
        self.dimension_name = dimension_name


class StringDimensionRepositoryUnavailable(RuntimeError):
    """String Dimension persistence is temporarily unavailable."""


@dataclass(frozen=True, slots=True)
class StringDimensionCursor:
    """Decoded keyset position for descending creation order."""

    created_at: datetime
    id: UUID

    def __post_init__(self) -> None:
        if self.created_at.utcoffset() is None:
            raise ValueError("string Dimension cursor timestamp must be timezone-aware")
        if not isinstance(self.id, UUID):
            raise ValueError("string Dimension cursor id must be a UUID")


@dataclass(frozen=True, slots=True)
class StringDimensionPage[ValueT]:
    """One bounded page plus whether another keyset page exists."""

    items: tuple[Dimension[ValueT], ...]
    has_more: bool


class StringDimensionRepository[ValueT](Protocol):
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
        after: StringDimensionCursor | None,
        limit: int,
    ) -> StringDimensionPage[ValueT]: ...

    async def update_value(
        self,
        dimension_id: UUID,
        expected_version: int,
        value: ValueT | None,
        audit: MutationAuditMetadata,
        *,
        code: DimensionCode | None = None,
    ) -> Dimension[ValueT]: ...


class ModelRepository(StringDimensionRepository[ModelValue], Protocol):
    """Persistence boundary for Model Dimensions."""


class BrandRepository(StringDimensionRepository[BrandValue], Protocol):
    """Persistence boundary for Brand Dimensions."""


class CountryRepository(StringDimensionRepository[CountryValue], Protocol):
    """Persistence boundary for Country Dimensions."""


class CategoryRepository(StringDimensionRepository[CategoryValue], Protocol):
    """Persistence boundary for Category Dimensions."""


class _CreateStringDimension[ValueT]:
    def __init__(
        self,
        repository: StringDimensionRepository[ValueT],
        authorization: AuthorizationPolicy,
        audit_factory: HumanMutationAuditFactory,
        value_type: Callable[[str], ValueT],
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
        value: str,
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


class _UpdateStringDimensionValue[ValueT]:
    def __init__(
        self,
        repository: StringDimensionRepository[ValueT],
        authorization: AuthorizationPolicy,
        audit_factory: HumanMutationAuditFactory,
        value_type: Callable[[str], ValueT],
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
        value: str | None = None,
        code: str | None = None,
        reason: str | None = None,
    ) -> Dimension[ValueT]:
        self._authorization.authorize(principal, AuthorizationAction.MUTATE_DATA)
        if type(expected_version) is not int or expected_version < 1:
            raise ValueError("expected version must be a positive integer")
        if code is None and value is None:
            raise ValueError("code or value is required")
        normalized_code = None if code is None else DimensionCode(code)
        normalized_value = None if value is None else self._value_type(value)
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


class _GetStringDimension[ValueT]:
    def __init__(
        self,
        repository: StringDimensionRepository[ValueT],
        authorization: AuthorizationPolicy,
    ) -> None:
        self._repository = repository
        self._authorization = authorization

    async def execute(self, principal: HumanPrincipal, dimension_id: UUID) -> Dimension[ValueT]:
        self._authorization.authorize(principal, AuthorizationAction.READ_DATA)
        return await self._repository.get_active(dimension_id)


class _ListStringDimensions[ValueT]:
    def __init__(
        self,
        repository: StringDimensionRepository[ValueT],
        authorization: AuthorizationPolicy,
    ) -> None:
        self._repository = repository
        self._authorization = authorization

    async def execute(
        self,
        principal: HumanPrincipal,
        *,
        after: StringDimensionCursor | None,
        limit: int,
    ) -> StringDimensionPage[ValueT]:
        self._authorization.authorize(principal, AuthorizationAction.READ_DATA)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("string Dimension page limit must be between 1 and 100")
        return await self._repository.list_active(after=after, limit=limit)


class CreateModel(_CreateStringDimension[ModelValue]):
    def __init__(
        self,
        repository: ModelRepository,
        authorization: AuthorizationPolicy,
        audit_factory: HumanMutationAuditFactory,
    ) -> None:
        super().__init__(repository, authorization, audit_factory, ModelValue)


class UpdateModelValue(_UpdateStringDimensionValue[ModelValue]):
    def __init__(
        self,
        repository: ModelRepository,
        authorization: AuthorizationPolicy,
        audit_factory: HumanMutationAuditFactory,
    ) -> None:
        super().__init__(repository, authorization, audit_factory, ModelValue)


class GetModel(_GetStringDimension[ModelValue]):
    pass


class ListModels(_ListStringDimensions[ModelValue]):
    pass


class CreateBrand(_CreateStringDimension[BrandValue]):
    def __init__(
        self,
        repository: BrandRepository,
        authorization: AuthorizationPolicy,
        audit_factory: HumanMutationAuditFactory,
    ) -> None:
        super().__init__(repository, authorization, audit_factory, BrandValue)


class UpdateBrandValue(_UpdateStringDimensionValue[BrandValue]):
    def __init__(
        self,
        repository: BrandRepository,
        authorization: AuthorizationPolicy,
        audit_factory: HumanMutationAuditFactory,
    ) -> None:
        super().__init__(repository, authorization, audit_factory, BrandValue)


class GetBrand(_GetStringDimension[BrandValue]):
    pass


class ListBrands(_ListStringDimensions[BrandValue]):
    pass


class CreateCountry(_CreateStringDimension[CountryValue]):
    def __init__(
        self,
        repository: CountryRepository,
        authorization: AuthorizationPolicy,
        audit_factory: HumanMutationAuditFactory,
    ) -> None:
        super().__init__(repository, authorization, audit_factory, CountryValue)


class UpdateCountryValue(_UpdateStringDimensionValue[CountryValue]):
    def __init__(
        self,
        repository: CountryRepository,
        authorization: AuthorizationPolicy,
        audit_factory: HumanMutationAuditFactory,
    ) -> None:
        super().__init__(repository, authorization, audit_factory, CountryValue)


class GetCountry(_GetStringDimension[CountryValue]):
    pass


class ListCountries(_ListStringDimensions[CountryValue]):
    pass


class CreateCategory(_CreateStringDimension[CategoryValue]):
    def __init__(
        self,
        repository: CategoryRepository,
        authorization: AuthorizationPolicy,
        audit_factory: HumanMutationAuditFactory,
    ) -> None:
        super().__init__(repository, authorization, audit_factory, CategoryValue)


class UpdateCategoryValue(_UpdateStringDimensionValue[CategoryValue]):
    def __init__(
        self,
        repository: CategoryRepository,
        authorization: AuthorizationPolicy,
        audit_factory: HumanMutationAuditFactory,
    ) -> None:
        super().__init__(repository, authorization, audit_factory, CategoryValue)


class GetCategory(_GetStringDimension[CategoryValue]):
    pass


class ListCategories(_ListStringDimensions[CategoryValue]):
    pass
