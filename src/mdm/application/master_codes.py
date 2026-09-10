"""MasterCode use cases and persistence ports."""

from dataclasses import dataclass, fields
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
    CompanyValue,
    CountryValue,
    DimensionCode,
    DimensionValidationError,
    MemoryValue,
    ModelValue,
    NetworkGeneration,
    YearValue,
)
from mdm.domain.master_codes import MasterCode


class MasterCodeNotFound(RuntimeError):
    """No active MasterCode exists for the requested identifier."""


class MasterCodeConflict(RuntimeError):
    """The composed code or complete reference tuple is already reserved."""


class InvalidDimensionReference(RuntimeError):
    """One or more existing Dimension references are missing or inactive."""

    def __init__(self, fields: tuple[str, ...]) -> None:
        super().__init__()
        self.fields = fields


class InlineDimensionConflict(RuntimeError):
    """An inline Dimension code, value, or both conflict with stored state."""

    def __init__(self, field: str, *, code: bool, value: bool) -> None:
        super().__init__()
        self.field = field
        self.code = code
        self.value = value


class MasterCodeRepositoryUnavailable(RuntimeError):
    """MasterCode persistence is temporarily unavailable."""


@dataclass(frozen=True, slots=True)
class ExistingDimension:
    """Use one active stored Dimension by identifier."""

    id: UUID

    def __post_init__(self) -> None:
        if not isinstance(self.id, UUID):
            raise ValueError("Dimension reference id must be a UUID")


@dataclass(frozen=True, slots=True)
class MemoryCreateValue:
    """Untrusted Memory value awaiting domain normalization."""

    amount: int
    unit: str


@dataclass(frozen=True, slots=True)
class InlineDimension:
    """Create one typed Dimension in the MasterCode transaction."""

    code: str | DimensionCode
    value: object


@dataclass(frozen=True, slots=True)
class NotApplicable:
    """Resolve one MasterCode slot to SQL NULL and the NNN code part."""


DimensionSelection = ExistingDimension | InlineDimension | NotApplicable


@dataclass(frozen=True, slots=True)
class MasterCodeCreateInput:
    """The eight required untrusted selections accepted by the use case."""

    company: DimensionSelection
    brand: DimensionSelection
    model: DimensionSelection
    category: DimensionSelection
    year: DimensionSelection
    memory: DimensionSelection
    network: DimensionSelection
    country: DimensionSelection


@dataclass(frozen=True, slots=True)
class MasterCodeCreatePlan:
    """A normalized plan whose inline values retain their concrete type."""

    company: DimensionSelection
    brand: DimensionSelection
    model: DimensionSelection
    category: DimensionSelection
    year: DimensionSelection
    memory: DimensionSelection
    network: DimensionSelection
    country: DimensionSelection

    def __post_init__(self) -> None:
        for field in fields(self):
            if not isinstance(getattr(self, field.name), DimensionSelection):
                raise ValueError(f"{field.name} selection is invalid")


@dataclass(frozen=True, slots=True)
class MasterCodeCursor:
    """Decoded keyset position for descending MasterCode creation order."""

    created_at: datetime
    id: UUID

    def __post_init__(self) -> None:
        if self.created_at.utcoffset() is None:
            raise ValueError("MasterCode cursor timestamp must be timezone-aware")
        if not isinstance(self.id, UUID):
            raise ValueError("MasterCode cursor id must be a UUID")


@dataclass(frozen=True, slots=True)
class MasterCodePage:
    """One bounded MasterCode page and whether a following page exists."""

    items: tuple[MasterCode, ...]
    has_more: bool


class MasterCodeRepository(Protocol):
    """Persistence operations required by the create/read vertical slice."""

    async def create(
        self,
        plan: MasterCodeCreatePlan,
        audit: MutationAuditMetadata,
    ) -> MasterCode: ...

    async def get_active(self, master_code_id: UUID) -> MasterCode: ...

    async def list_active(
        self,
        *,
        after: MasterCodeCursor | None,
        limit: int,
    ) -> MasterCodePage: ...


class CreateMasterCode:
    """Authorize and atomically resolve all slots before MasterCode creation."""

    def __init__(
        self,
        repository: MasterCodeRepository,
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
        dimensions: MasterCodeCreateInput,
        reason: str | None,
    ) -> MasterCode:
        self._authorization.authorize(principal, AuthorizationAction.MUTATE_DATA)
        plan = _normalize_plan(dimensions)
        has_inline = any(
            isinstance(getattr(plan, field.name), InlineDimension) for field in fields(plan)
        )
        try:
            audit = self._audit_factory.create(
                principal,
                dimension_operation=DimensionOperation.CREATE if has_inline else None,
                master_code_operation=MasterCodeOperation.CREATE,
                reason=reason,
            )
        except AuditInvariantError as error:
            raise DimensionValidationError(
                "reason", "유효한 생성 이유를 입력해야 합니다."
            ) from error
        return await self._repository.create(plan, audit)


class GetMasterCode:
    """Read one active MasterCode after applying the HUMAN read policy."""

    def __init__(
        self, repository: MasterCodeRepository, authorization: AuthorizationPolicy
    ) -> None:
        self._repository = repository
        self._authorization = authorization

    async def execute(self, principal: HumanPrincipal, master_code_id: UUID) -> MasterCode:
        self._authorization.authorize(principal, AuthorizationAction.READ_DATA)
        return await self._repository.get_active(master_code_id)


class ListMasterCodes:
    """Read one stable cursor page of active MasterCodes."""

    def __init__(
        self, repository: MasterCodeRepository, authorization: AuthorizationPolicy
    ) -> None:
        self._repository = repository
        self._authorization = authorization

    async def execute(
        self,
        principal: HumanPrincipal,
        *,
        after: MasterCodeCursor | None,
        limit: int,
    ) -> MasterCodePage:
        self._authorization.authorize(principal, AuthorizationAction.READ_DATA)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("MasterCode page limit must be between 1 and 100")
        return await self._repository.list_active(after=after, limit=limit)


_STRING_VALUE_TYPES = {
    "company": CompanyValue,
    "brand": BrandValue,
    "model": ModelValue,
    "category": CategoryValue,
    "country": CountryValue,
}


def _normalize_plan(value: MasterCodeCreateInput) -> MasterCodeCreatePlan:
    normalized: dict[str, DimensionSelection] = {}
    for field in fields(value):
        slot = field.name
        selection = getattr(value, slot)
        if isinstance(selection, (ExistingDimension, NotApplicable)):
            normalized[slot] = selection
            continue
        if not isinstance(selection, InlineDimension):
            raise DimensionValidationError(f"dimensions.{slot}", "유효한 mode를 입력해야 합니다.")
        try:
            code = (
                selection.code
                if isinstance(selection.code, DimensionCode)
                else DimensionCode(selection.code)
            )
            dimension_value = _normalize_inline_value(slot, selection.value)
        except DimensionValidationError as error:
            raise DimensionValidationError(
                f"dimensions.{slot}.{error.field}", error.message
            ) from error
        normalized[slot] = InlineDimension(code=code, value=dimension_value)
    return MasterCodeCreatePlan(**normalized)


def _normalize_inline_value(slot: str, value: object) -> object:
    string_type = _STRING_VALUE_TYPES.get(slot)
    if string_type is not None:
        return string_type(value)  # type: ignore[bad-argument-type]
    if slot == "year":
        return YearValue(value)  # type: ignore[bad-argument-type]
    if slot == "network":
        return NetworkGeneration(value)
    if slot == "memory":
        if not isinstance(value, MemoryCreateValue):
            raise DimensionValidationError("value", "amount와 unit을 입력해야 합니다.")
        return MemoryValue.create(amount=value.amount, unit=value.unit)
    raise ValueError(f"unsupported MasterCode Dimension slot: {slot}")
