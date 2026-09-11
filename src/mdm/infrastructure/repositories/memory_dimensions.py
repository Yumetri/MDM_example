"""Async SQLAlchemy repository for Memory Dimensions."""

from typing import Any
from uuid import UUID

from sqlalchemy import and_, or_, select
from sqlalchemy.exc import (
    DisconnectionError,
    IntegrityError,
    OperationalError,
    SQLAlchemyError,
)
from sqlalchemy.exc import TimeoutError as SQLAlchemyTimeoutError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mdm.application.audit import MutationAuditContextUnavailable
from mdm.application.master_codes import MasterCodeConflict
from mdm.application.memory_dimensions import (
    MemoryDimensionCodeConflict,
    MemoryDimensionCursor,
    MemoryDimensionMultipleConflicts,
    MemoryDimensionNotFound,
    MemoryDimensionPage,
    MemoryDimensionRepositoryUnavailable,
    MemoryDimensionValueConflict,
)
from mdm.application.preconditions import PreconditionFailed
from mdm.domain.audit import MutationAuditMetadata
from mdm.domain.dimensions import Dimension, DimensionCode, MemoryUnit, MemoryValue
from mdm.infrastructure.audit_context import SqlAlchemyMutationAuditContextWriter
from mdm.infrastructure.models import MemoryRecord
from mdm.infrastructure.repositories.master_codes import (
    lock_referencing_master_codes,
    recompose_locked_master_codes,
)


class SqlAlchemyMemoryRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def create(
        self,
        code: DimensionCode,
        value: MemoryValue,
        audit: MutationAuditMetadata,
    ) -> Dimension[MemoryValue]:
        try:
            async with self._session_factory.begin() as session:
                conflicts = (
                    await session.execute(
                        select(MemoryRecord.code, MemoryRecord.capacity_mb).where(
                            or_(
                                MemoryRecord.code == code.value,
                                MemoryRecord.capacity_mb == value.capacity_mb,
                            )
                        )
                    )
                ).all()
                code_conflict = any(row.code == code.value for row in conflicts)
                value_conflict = any(row.capacity_mb == value.capacity_mb for row in conflicts)
                _raise_conflict(
                    code_conflict=code_conflict,
                    value_conflict=value_conflict,
                )

                await SqlAlchemyMutationAuditContextWriter(session).set_context(audit)
                record = MemoryRecord(
                    code=code.value,
                    amount=value.amount,
                    unit=value.unit.value,
                )
                session.add(record)
                await session.flush()
                await session.refresh(record)
                return _to_domain(record)
        except IntegrityError as error:
            constraint_name = _constraint_name(error)
            if constraint_name == "uq_dimension_memories_code":
                raise MemoryDimensionCodeConflict from None
            if constraint_name == "uq_dimension_memories_capacity_mb":
                raise MemoryDimensionValueConflict from None
            raise
        except MutationAuditContextUnavailable:
            raise MemoryDimensionRepositoryUnavailable from None
        except SQLAlchemyError as error:
            _raise_if_temporarily_unavailable(error)
            raise

    async def get_active(self, dimension_id: UUID) -> Dimension[MemoryValue]:
        try:
            async with self._session_factory() as session:
                record = await session.scalar(
                    select(MemoryRecord).where(
                        MemoryRecord.id == dimension_id,
                        MemoryRecord.deleted_at.is_(None),
                    )
                )
        except SQLAlchemyError as error:
            _raise_if_temporarily_unavailable(error)
            raise
        if record is None:
            raise MemoryDimensionNotFound
        return _to_domain(record)

    async def update_value(
        self,
        dimension_id: UUID,
        expected_version: int,
        value: MemoryValue | None,
        audit: MutationAuditMetadata,
        *,
        code: DimensionCode | None = None,
    ) -> Dimension[MemoryValue]:
        try:
            async with self._session_factory.begin() as session:
                record = await session.scalar(
                    select(MemoryRecord)
                    .where(
                        MemoryRecord.id == dimension_id,
                        MemoryRecord.deleted_at.is_(None),
                    )
                    .with_for_update()
                )
                if record is None:
                    raise MemoryDimensionNotFound
                current = _to_domain(record)
                if current.version != expected_version:
                    raise PreconditionFailed
                code_changed = code is not None and code != current.code
                value_changed = value is not None and value.capacity_mb != current.value.capacity_mb
                if not code_changed and not value_changed:
                    return current

                conflict_conditions = []
                if code_changed:
                    assert code is not None
                    conflict_conditions.append(MemoryRecord.code == code.value)
                if value_changed:
                    assert value is not None
                    conflict_conditions.append(MemoryRecord.capacity_mb == value.capacity_mb)
                conflicts = (
                    await session.execute(
                        select(MemoryRecord.code, MemoryRecord.capacity_mb).where(
                            MemoryRecord.id != dimension_id,
                            or_(*conflict_conditions),
                        )
                    )
                ).all()
                _raise_conflict(
                    code_conflict=(
                        code_changed
                        and code is not None
                        and any(row.code == code.value for row in conflicts)
                    ),
                    value_conflict=(
                        value_changed
                        and value is not None
                        and any(row.capacity_mb == value.capacity_mb for row in conflicts)
                    ),
                )

                locked_master_codes = (
                    await lock_referencing_master_codes(
                        session,
                        slot="memory",
                        dimension_id=dimension_id,
                    )
                    if code_changed
                    else ()
                )
                minimum_timestamp = max(
                    (current.updated_at, *(item.current.updated_at for item in locked_master_codes))
                )

                changed_at = await SqlAlchemyMutationAuditContextWriter(session).set_context(
                    audit, minimum_timestamp=minimum_timestamp
                )
                changed = current.change(code=code, value=value, changed_at=changed_at)
                if code_changed:
                    recompose_locked_master_codes(
                        locked_master_codes,
                        slot="memory",
                        dimension=changed,
                        changed_at=changed_at,
                    )
                    record.code = changed.code.value
                if value_changed:
                    record.amount = changed.value.amount
                    record.unit = changed.value.unit.value
                record.version = changed.version
                record.updated_at = changed.updated_at
                await session.flush()
                await session.refresh(record)
                return _to_domain(record)
        except IntegrityError as error:
            constraint_name = _constraint_name(error)
            if constraint_name == "uq_dimension_memories_code":
                raise MemoryDimensionCodeConflict from None
            if constraint_name == "uq_dimension_memories_capacity_mb":
                raise MemoryDimensionValueConflict from None
            if constraint_name in {"uq_master_codes_code", "uq_master_codes_references"}:
                raise MasterCodeConflict from None
            raise
        except MutationAuditContextUnavailable:
            raise MemoryDimensionRepositoryUnavailable from None
        except SQLAlchemyError as error:
            _raise_if_temporarily_unavailable(error)
            raise

    async def list_active(
        self,
        *,
        after: MemoryDimensionCursor | None,
        limit: int,
    ) -> MemoryDimensionPage:
        statement = select(MemoryRecord).where(MemoryRecord.deleted_at.is_(None))
        if after is not None:
            statement = statement.where(
                or_(
                    MemoryRecord.created_at < after.created_at,
                    and_(
                        MemoryRecord.created_at == after.created_at,
                        MemoryRecord.id < after.id,
                    ),
                )
            )
        statement = statement.order_by(
            MemoryRecord.created_at.desc(), MemoryRecord.id.desc()
        ).limit(limit + 1)
        try:
            async with self._session_factory() as session:
                records = (await session.scalars(statement)).all()
        except SQLAlchemyError as error:
            _raise_if_temporarily_unavailable(error)
            raise
        return MemoryDimensionPage(
            items=tuple(_to_domain(record) for record in records[:limit]),
            has_more=len(records) > limit,
        )


def _raise_conflict(*, code_conflict: bool, value_conflict: bool) -> None:
    if code_conflict and value_conflict:
        raise MemoryDimensionMultipleConflicts
    if code_conflict:
        raise MemoryDimensionCodeConflict
    if value_conflict:
        raise MemoryDimensionValueConflict


def _to_domain(record: MemoryRecord) -> Dimension[MemoryValue]:
    value = MemoryValue(amount=record.amount, unit=MemoryUnit(record.unit))
    if value.capacity_mb != record.capacity_mb:
        raise ValueError("stored Memory capacity does not match amount and unit")
    return Dimension(
        id=record.id,
        code=DimensionCode(record.code),
        value=value,
        version=record.version,
        created_at=record.created_at,
        updated_at=record.updated_at,
        deleted_at=record.deleted_at,
    )


def _constraint_name(error: IntegrityError) -> str | None:
    current: Any = error.orig
    for _ in range(3):
        name = getattr(current, "constraint_name", None)
        if isinstance(name, str):
            return name
        current = getattr(current, "__cause__", None)
        if current is None:
            break
    return None


_TRANSIENT_SQLSTATES = {"40001", "40P01", "55P03", "57014"}


def _raise_if_temporarily_unavailable(error: SQLAlchemyError) -> None:
    if isinstance(error, (OperationalError, DisconnectionError, SQLAlchemyTimeoutError)):
        raise MemoryDimensionRepositoryUnavailable from None
    sqlstate = _sqlstate(error)
    if sqlstate is not None and (sqlstate.startswith("08") or sqlstate in _TRANSIENT_SQLSTATES):
        raise MemoryDimensionRepositoryUnavailable from None


def _sqlstate(error: SQLAlchemyError) -> str | None:
    current: Any = error
    for _ in range(5):
        for attribute in ("sqlstate", "pgcode"):
            value = getattr(current, attribute, None)
            if isinstance(value, str):
                return value
        current = getattr(current, "__cause__", None) or getattr(current, "orig", None)
        if current is None:
            break
    return None
