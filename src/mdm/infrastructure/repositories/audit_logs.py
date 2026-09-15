"""Single-statement, bounded keyset reads across immutable audit tables."""

from typing import Any, cast
from uuid import UUID

from asyncpg import PostgresError
from sqlalchemy import Select, String, literal, select, tuple_, union_all
from sqlalchemy.exc import DBAPIError, DisconnectionError, OperationalError, SQLAlchemyError
from sqlalchemy.exc import TimeoutError as SQLAlchemyTimeoutError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mdm.application.audit_logs import (
    AuditField,
    AuditLogCursor,
    AuditLogPage,
    AuditLogQuery,
    AuditLogRepositoryUnavailable,
    AuditSource,
    DimensionAuditLog,
    DimensionLogValue,
    LogEntry,
    MasterCodeAuditLog,
    MasterCodeLogState,
    MemoryLogValue,
)
from mdm.domain.audit import ActorKind, DimensionOperation, MasterCodeOperation
from mdm.domain.auth import UserRole
from mdm.domain.dimensions import MemoryUnit
from mdm.infrastructure.models import (
    BrandLogRecord,
    CategoryLogRecord,
    CompanyLogRecord,
    CountryLogRecord,
    MasterCodeLogRecord,
    MemoryLogRecord,
    ModelLogRecord,
    NetworkLogRecord,
    YearLogRecord,
)

_RECORDS = {
    AuditSource.COMPANY: CompanyLogRecord,
    AuditSource.MODEL: ModelLogRecord,
    AuditSource.BRAND: BrandLogRecord,
    AuditSource.COUNTRY: CountryLogRecord,
    AuditSource.CATEGORY: CategoryLogRecord,
    AuditSource.YEAR: YearLogRecord,
    AuditSource.NETWORK: NetworkLogRecord,
    AuditSource.MEMORY: MemoryLogRecord,
    AuditSource.MASTER_CODE: MasterCodeLogRecord,
}


def _source_statement(
    source: AuditSource, query: AuditLogQuery, after: AuditLogCursor | None, limit: int
) -> Select[Any]:
    table = _RECORDS[source].__table__
    c = table.c
    master = source is AuditSource.MASTER_CODE
    entity = c.master_code_id if master else c.dimension_id
    version = c.master_code_version if master else c.dimension_version
    old = c.old_state if master else c.old_value
    new = c.new_state if master else c.new_value
    statement = select(
        literal(source.kind, String).label("source_kind"),
        literal(source.value, String).label("source_type"),
        c.id,
        c.change_set_id,
        c.changed_at,
        c.actor_kind,
        c.actor_id,
        c.actor_role,
        c.reason,
        c.operation,
        entity.label("entity_id"),
        version.label("version"),
        (literal(None, String) if master else c.field_name).label("field_name"),
        old.label("old_value"),
        new.label("new_value"),
    )
    if query.change_set_id is not None:
        statement = statement.where(c.change_set_id == query.change_set_id)
    filters = query.filters
    if filters.entity_id is not None:
        statement = statement.where(entity == filters.entity_id)
    if filters.changed_from is not None:
        statement = statement.where(c.changed_at >= filters.changed_from)
    if filters.changed_before is not None:
        statement = statement.where(c.changed_at < filters.changed_before)
    for name in ("actor_id", "actor_kind", "actor_role", "operation"):
        value = getattr(filters, name)
        if value is not None:
            statement = statement.where(c[name] == value)
    if filters.field_name is not None:
        statement = statement.where(c.field_name == filters.field_name)
    if after is not None:
        source_key = (source.kind, source.value)
        cursor_key = (after.source_type.kind, after.source_type.value)
        if source_key == cursor_key:
            statement = statement.where(
                tuple_(c.changed_at, c.id) < tuple_(literal(after.changed_at), literal(after.id))
            )
        elif source_key > cursor_key:
            statement = statement.where(c.changed_at <= after.changed_at)
        else:
            statement = statement.where(c.changed_at < after.changed_at)
    return statement.order_by(c.changed_at.desc(), c.id.desc()).limit(limit + 1)


def build_audit_log_statement(
    query: AuditLogQuery, *, after: AuditLogCursor | None, limit: int
) -> Select[Any]:
    """Keep each union branch bounded and retain one PostgreSQL statement snapshot."""
    if query.source is not None:
        return _source_statement(query.source, query, after, limit)
    branches = [
        select(_source_statement(source, query, after, limit).subquery()) for source in AuditSource
    ]
    logs = union_all(*branches).subquery("audit_logs")
    return (
        select(logs)
        .order_by(
            logs.c.changed_at.desc(),
            logs.c.source_kind.collate("C").asc(),
            logs.c.source_type.collate("C").asc(),
            logs.c.id.desc(),
        )
        .limit(limit + 1)
    )


def _memory_value(value: Any) -> DimensionLogValue:
    if isinstance(value, dict):
        return MemoryLogValue(value["amount"], MemoryUnit(value["unit"]), value["capacity_mb"])
    return cast(DimensionLogValue, value)


def _state(value: Any) -> MasterCodeLogState:
    fields = {
        name: (UUID(identifier) if identifier is not None else None)
        for name, identifier in value.items()
        if name.endswith("_id")
    }
    return MasterCodeLogState(**fields, code=value["code"], deleted=value["deleted"])


def _entry(row: Any) -> LogEntry:
    source = AuditSource(row["source_type"])
    common = dict(
        id=row["id"],
        source_type=source,
        change_set_id=row["change_set_id"],
        changed_at=row["changed_at"],
        actor_kind=ActorKind(row["actor_kind"]),
        actor_id=row["actor_id"],
        actor_role=UserRole(row["actor_role"]) if row["actor_role"] is not None else None,
        reason=row["reason"],
    )
    if source is AuditSource.MASTER_CODE:
        return MasterCodeAuditLog(
            **common,
            master_code_id=row["entity_id"],
            master_code_version=row["version"],
            operation=MasterCodeOperation(row["operation"]),
            old_state=None if row["old_value"] is None else _state(row["old_value"]),
            new_state=_state(row["new_value"]),
        )
    return DimensionAuditLog(
        **common,
        dimension_id=row["entity_id"],
        dimension_version=row["version"],
        operation=DimensionOperation(row["operation"]),
        field_name=cast(AuditField, row["field_name"]),
        old_value=_memory_value(row["old_value"]),
        new_value=_memory_value(row["new_value"]),
    )


def _transient_sqlstate(error: SQLAlchemyError | PostgresError) -> bool:
    current: Any = error
    for _ in range(5):
        for attribute in ("sqlstate", "pgcode"):
            value = getattr(current, attribute, None)
            if isinstance(value, str):
                return value.startswith("08") or value in {
                    "40001",
                    "40P01",
                    "53300",
                    "55P03",
                    "57014",
                    "57P01",
                    "57P02",
                    "57P03",
                }
        current = getattr(current, "__cause__", None) or getattr(current, "orig", None)
        if current is None:
            break
    return False


class SqlAlchemyAuditLogRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]):
        self._session_factory = session_factory

    async def list_logs(
        self, query: AuditLogQuery, *, after: AuditLogCursor | None, limit: int
    ) -> AuditLogPage:
        statement = build_audit_log_statement(query, after=after, limit=limit)
        try:
            async with self._session_factory() as session:
                rows = (await session.execute(statement)).mappings().all()
        except OSError:
            raise AuditLogRepositoryUnavailable from None
        except (SQLAlchemyError, PostgresError) as error:
            if (
                isinstance(error, (OperationalError, DisconnectionError, SQLAlchemyTimeoutError))
                or (isinstance(error, DBAPIError) and error.connection_invalidated)
                or _transient_sqlstate(error)
            ):
                raise AuditLogRepositoryUnavailable from None
            raise
        return AuditLogPage(tuple(_entry(row) for row in rows[:limit]), len(rows) > limit)
