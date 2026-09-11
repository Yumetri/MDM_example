"""Company Dimension use cases and persistence ports."""

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
    CompanyValue,
    Dimension,
    DimensionCode,
    DimensionValidationError,
)


class CompanyNotFound(RuntimeError):
    """No active Company exists for a requested identifier."""


class CompanyCodeConflict(RuntimeError):
    """The normalized Company code is already reserved."""


class CompanyValueConflict(RuntimeError):
    """The normalized Company value is already reserved."""


class CompanyMultipleConflicts(RuntimeError):
    """Both normalized Company code and value are already reserved."""


class CompanyRepositoryUnavailable(RuntimeError):
    """Company persistence is temporarily unavailable without exposing diagnostics."""


@dataclass(frozen=True, slots=True)
class CompanyCursor:
    """Decoded keyset position for descending Company creation order."""

    created_at: datetime
    id: UUID

    def __post_init__(self) -> None:
        if self.created_at.utcoffset() is None:
            raise ValueError("company cursor timestamp must be timezone-aware")
        if not isinstance(self.id, UUID):
            raise ValueError("company cursor id must be a UUID")


@dataclass(frozen=True, slots=True)
class CompanyPage:
    """One bounded page plus whether another keyset page exists."""

    items: tuple[Dimension[CompanyValue], ...]
    has_more: bool


class CompanyRepository(Protocol):
    """Persistence operations required by the Company vertical slice."""

    async def create(
        self,
        code: DimensionCode,
        value: CompanyValue,
        audit: MutationAuditMetadata,
    ) -> Dimension[CompanyValue]:
        """Create one Company and its database-triggered audit records."""
        ...

    async def get_active(self, company_id: UUID) -> Dimension[CompanyValue]:
        """Return one active Company or raise CompanyNotFound."""
        ...

    async def list_active(
        self,
        *,
        after: CompanyCursor | None,
        limit: int,
    ) -> CompanyPage:
        """Return active Companies in descending (created_at, id) order."""
        ...

    async def update_value(
        self,
        company_id: UUID,
        expected_version: int,
        value: CompanyValue | None,
        audit: MutationAuditMetadata,
        *,
        code: DimensionCode | None = None,
    ) -> Dimension[CompanyValue]:
        """Conditionally replace one active Company's code and/or value."""
        ...


class CreateCompany:
    """Authorize and create one normalized Company with trusted audit metadata."""

    def __init__(
        self,
        repository: CompanyRepository,
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
        value: str,
        reason: str | None,
    ) -> Dimension[CompanyValue]:
        self._authorization.authorize(principal, AuthorizationAction.MUTATE_DATA)
        normalized_code = DimensionCode(code)
        normalized_value = CompanyValue(value)
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


class UpdateCompanyValue:
    """Authorize and conditionally replace one Company's normalized value."""

    def __init__(
        self,
        repository: CompanyRepository,
        authorization: AuthorizationPolicy,
        audit_factory: HumanMutationAuditFactory,
    ) -> None:
        self._repository = repository
        self._authorization = authorization
        self._audit_factory = audit_factory

    async def execute(
        self,
        principal: HumanPrincipal,
        company_id: UUID,
        *,
        expected_version: int,
        value: str | None = None,
        code: str | None = None,
        reason: str | None = None,
    ) -> Dimension[CompanyValue]:
        self._authorization.authorize(principal, AuthorizationAction.MUTATE_DATA)
        if type(expected_version) is not int or expected_version < 1:
            raise ValueError("expected version must be a positive integer")
        if code is None and value is None:
            raise ValueError("code or value is required")
        normalized_code = None if code is None else DimensionCode(code)
        normalized_value = None if value is None else CompanyValue(value)
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
            company_id,
            expected_version,
            normalized_value,
            audit,
            code=normalized_code,
        )


class GetCompany:
    """Read one active Company after applying the HUMAN read policy."""

    def __init__(self, repository: CompanyRepository, authorization: AuthorizationPolicy) -> None:
        self._repository = repository
        self._authorization = authorization

    async def execute(
        self,
        principal: HumanPrincipal,
        company_id: UUID,
    ) -> Dimension[CompanyValue]:
        self._authorization.authorize(principal, AuthorizationAction.READ_DATA)
        return await self._repository.get_active(company_id)


class ListCompanies:
    """Read one stable cursor page of active Companies."""

    def __init__(self, repository: CompanyRepository, authorization: AuthorizationPolicy) -> None:
        self._repository = repository
        self._authorization = authorization

    async def execute(
        self,
        principal: HumanPrincipal,
        *,
        after: CompanyCursor | None,
        limit: int,
    ) -> CompanyPage:
        self._authorization.authorize(principal, AuthorizationAction.READ_DATA)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("company page limit must be between 1 and 100")
        return await self._repository.list_active(after=after, limit=limit)
