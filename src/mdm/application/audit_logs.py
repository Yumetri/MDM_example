"""Read-only audit queries, typed snapshots and framework-independent ports."""

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal, Protocol
from uuid import UUID

from mdm.application.auth import HumanPrincipal
from mdm.application.authorization import AuthorizationAction, AuthorizationPolicy
from mdm.domain.audit import ActorKind, DimensionOperation, MasterCodeOperation
from mdm.domain.auth import UserRole
from mdm.domain.dimensions import DimensionValidationError, MemoryUnit


class AuditSource(StrEnum):
    COMPANY = "COMPANY"
    MODEL = "MODEL"
    BRAND = "BRAND"
    COUNTRY = "COUNTRY"
    CATEGORY = "CATEGORY"
    YEAR = "YEAR"
    NETWORK = "NETWORK"
    MEMORY = "MEMORY"
    MASTER_CODE = "MASTER_CODE"

    @property
    def kind(self) -> Literal["DIMENSION", "MASTER_CODE"]:
        return "MASTER_CODE" if self is AuditSource.MASTER_CODE else "DIMENSION"


type AuditField = Literal["CODE", "VALUE", "DELETED"]


@dataclass(frozen=True, slots=True)
class MemoryLogValue:
    amount: int
    unit: MemoryUnit
    capacity_mb: int


type DimensionLogValue = str | int | bool | MemoryLogValue | None


@dataclass(frozen=True, slots=True)
class MasterCodeLogState:
    company_id: UUID | None
    brand_id: UUID | None
    model_id: UUID | None
    category_id: UUID | None
    year_id: UUID | None
    memory_id: UUID | None
    network_id: UUID | None
    country_id: UUID | None
    code: str
    deleted: bool


@dataclass(frozen=True, slots=True)
class AuditLog:
    id: UUID
    source_type: AuditSource
    change_set_id: UUID
    changed_at: datetime
    actor_kind: ActorKind
    actor_id: str
    actor_role: UserRole | None
    reason: str | None

    @property
    def source_kind(self) -> Literal["DIMENSION", "MASTER_CODE"]:
        return self.source_type.kind


@dataclass(frozen=True, slots=True)
class DimensionAuditLog(AuditLog):
    dimension_id: UUID
    dimension_version: int
    operation: DimensionOperation
    field_name: AuditField
    old_value: DimensionLogValue
    new_value: DimensionLogValue


@dataclass(frozen=True, slots=True)
class MasterCodeAuditLog(AuditLog):
    master_code_id: UUID
    master_code_version: int
    operation: MasterCodeOperation
    old_state: MasterCodeLogState | None
    new_state: MasterCodeLogState


type LogEntry = DimensionAuditLog | MasterCodeAuditLog


@dataclass(frozen=True, slots=True)
class AuditLogFilters:
    entity_id: UUID | None = None
    changed_from: datetime | None = None
    changed_before: datetime | None = None
    actor_id: str | None = None
    actor_kind: ActorKind | None = None
    actor_role: UserRole | None = None
    operation: DimensionOperation | MasterCodeOperation | None = None
    field_name: AuditField | None = None

    def __post_init__(self) -> None:
        if self.actor_id is not None and "\x00" in self.actor_id:
            raise DimensionValidationError("query.actor_id", "NUL 문자를 포함할 수 없습니다.")
        for field in ("changed_from", "changed_before"):
            value = getattr(self, field)
            if value is not None:
                if value.utcoffset() is None:
                    raise DimensionValidationError(f"query.{field}", "시간대를 포함해야 합니다.")
                try:
                    normalized = value.astimezone(UTC)
                except OverflowError:
                    raise DimensionValidationError(
                        f"query.{field}", "UTC로 표현할 수 있는 날짜·시각을 입력해야 합니다."
                    ) from None
                object.__setattr__(self, field, normalized)
        if (
            self.changed_from is not None
            and self.changed_before is not None
            and self.changed_from >= self.changed_before
        ):
            raise DimensionValidationError(
                "query.changed_before", "종료 시각은 시작 시각보다 늦어야 합니다."
            )


@dataclass(frozen=True, slots=True)
class AuditLogQuery:
    source: AuditSource | None = None
    change_set_id: UUID | None = None
    filters: AuditLogFilters = AuditLogFilters()

    def __post_init__(self) -> None:
        if self.source is None and (
            self.change_set_id is None or self.filters != AuditLogFilters()
        ):
            raise ValueError("combined audit queries require only a change set identifier")


@dataclass(frozen=True, slots=True)
class AuditLogCursor:
    changed_at: datetime
    source_type: AuditSource
    id: UUID

    def __post_init__(self) -> None:
        if self.changed_at.utcoffset() is None:
            raise ValueError("audit cursor requires a timezone")


@dataclass(frozen=True, slots=True)
class AuditLogPage:
    items: tuple[LogEntry, ...]
    has_more: bool


class AuditLogRepositoryUnavailable(RuntimeError):
    """Audit reads are temporarily unavailable, without database diagnostics."""


class AuditLogRepository(Protocol):
    async def list_logs(
        self,
        query: AuditLogQuery,
        *,
        after: AuditLogCursor | None,
        limit: int,
    ) -> AuditLogPage: ...


class ListAuditLogs:
    def __init__(self, repository: AuditLogRepository, authorization: AuthorizationPolicy):
        self._repository = repository
        self._authorization = authorization

    async def execute(
        self,
        principal: HumanPrincipal,
        query: AuditLogQuery,
        *,
        after: AuditLogCursor | None,
        limit: int,
    ) -> AuditLogPage:
        self._authorization.authorize(principal, AuthorizationAction.READ_AUDIT_LOG)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise DimensionValidationError("query.limit", "페이지 크기는 1~100이어야 합니다.")
        if after is not None and query.source is not None and after.source_type != query.source:
            raise DimensionValidationError(
                "query.cursor", "조회 대상과 cursor가 일치하지 않습니다."
            )
        return await self._repository.list_logs(query, after=after, limit=limit)
