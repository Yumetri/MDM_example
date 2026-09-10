"""Async SQLAlchemy Company Dimension repository."""

from typing import Any
from uuid import UUID

from sqlalchemy import and_, or_, select
from sqlalchemy.exc import (
    DisconnectionError,
    IntegrityError,
    OperationalError,
    SQLAlchemyError,
)
from sqlalchemy.exc import (
    TimeoutError as SQLAlchemyTimeoutError,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mdm.application.audit import MutationAuditContextUnavailable
from mdm.application.dimensions import (
    CompanyCodeConflict,
    CompanyCursor,
    CompanyMultipleConflicts,
    CompanyNotFound,
    CompanyPage,
    CompanyRepositoryUnavailable,
    CompanyValueConflict,
)
from mdm.application.preconditions import PreconditionFailed
from mdm.domain.audit import MutationAuditMetadata
from mdm.domain.dimensions import CompanyValue, Dimension, DimensionCode
from mdm.infrastructure.audit_context import SqlAlchemyMutationAuditContextWriter
from mdm.infrastructure.models import CompanyRecord


class SqlAlchemyCompanyRepository:
    """Persist Companies and let PostgreSQL enforce audit and uniqueness boundaries."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def create(
        self,
        code: DimensionCode,
        value: CompanyValue,
        audit: MutationAuditMetadata,
    ) -> Dimension[CompanyValue]:
        try:
            async with self._session_factory.begin() as session:
                conflicts = (
                    await session.execute(
                        select(CompanyRecord.code, CompanyRecord.value).where(
                            or_(
                                CompanyRecord.code == code.value,
                                CompanyRecord.value == value.value,
                            )
                        )
                    )
                ).all()
                code_conflict = any(row.code == code.value for row in conflicts)
                value_conflict = any(row.value == value.value for row in conflicts)
                _raise_conflict(code_conflict=code_conflict, value_conflict=value_conflict)

                await SqlAlchemyMutationAuditContextWriter(session).set_context(audit)
                record = CompanyRecord(code=code.value, value=value.value)
                session.add(record)
                await session.flush()
                return _to_domain(record)
        except IntegrityError as error:
            constraint_name = _constraint_name(error)
            if constraint_name == "uq_dimension_companies_code":
                raise CompanyCodeConflict from None
            if constraint_name == "uq_dimension_companies_value":
                raise CompanyValueConflict from None
            raise
        except MutationAuditContextUnavailable:
            raise CompanyRepositoryUnavailable from None
        except SQLAlchemyError as error:
            _raise_if_temporarily_unavailable(error)
            raise

    async def get_active(self, company_id: UUID) -> Dimension[CompanyValue]:
        try:
            async with self._session_factory() as session:
                record = await session.scalar(
                    select(CompanyRecord).where(
                        CompanyRecord.id == company_id,
                        CompanyRecord.deleted_at.is_(None),
                    )
                )
        except SQLAlchemyError as error:
            _raise_if_temporarily_unavailable(error)
            raise
        if record is None:
            raise CompanyNotFound
        return _to_domain(record)

    async def update_value(
        self,
        company_id: UUID,
        expected_version: int,
        value: CompanyValue,
        audit: MutationAuditMetadata,
    ) -> Dimension[CompanyValue]:
        try:
            async with self._session_factory.begin() as session:
                record = await session.scalar(
                    select(CompanyRecord)
                    .where(
                        CompanyRecord.id == company_id,
                        CompanyRecord.deleted_at.is_(None),
                    )
                    .with_for_update()
                )
                if record is None:
                    raise CompanyNotFound
                current = _to_domain(record)
                if current.version != expected_version:
                    raise PreconditionFailed
                if current.value == value:
                    return current
                conflict = await session.scalar(
                    select(CompanyRecord.id).where(
                        CompanyRecord.id != company_id,
                        CompanyRecord.value == value.value,
                    )
                )
                if conflict is not None:
                    raise CompanyValueConflict

                changed_at = await SqlAlchemyMutationAuditContextWriter(session).set_context(
                    audit, minimum_timestamp=current.updated_at
                )
                changed = current.change_value(value, changed_at=changed_at)
                record.value = changed.value.value
                record.version = changed.version
                record.updated_at = changed.updated_at
                await session.flush()
                return _to_domain(record)
        except IntegrityError as error:
            if _constraint_name(error) == "uq_dimension_companies_value":
                raise CompanyValueConflict from None
            raise
        except MutationAuditContextUnavailable:
            raise CompanyRepositoryUnavailable from None
        except SQLAlchemyError as error:
            _raise_if_temporarily_unavailable(error)
            raise

    async def list_active(
        self,
        *,
        after: CompanyCursor | None,
        limit: int,
    ) -> CompanyPage:
        statement = select(CompanyRecord).where(CompanyRecord.deleted_at.is_(None))
        if after is not None:
            statement = statement.where(
                or_(
                    CompanyRecord.created_at < after.created_at,
                    and_(
                        CompanyRecord.created_at == after.created_at,
                        CompanyRecord.id < after.id,
                    ),
                )
            )
        statement = statement.order_by(
            CompanyRecord.created_at.desc(), CompanyRecord.id.desc()
        ).limit(limit + 1)
        try:
            async with self._session_factory() as session:
                records = (await session.scalars(statement)).all()
        except SQLAlchemyError as error:
            _raise_if_temporarily_unavailable(error)
            raise
        has_more = len(records) > limit
        return CompanyPage(
            items=tuple(_to_domain(record) for record in records[:limit]),
            has_more=has_more,
        )


def _raise_conflict(*, code_conflict: bool, value_conflict: bool) -> None:
    if code_conflict and value_conflict:
        raise CompanyMultipleConflicts
    if code_conflict:
        raise CompanyCodeConflict
    if value_conflict:
        raise CompanyValueConflict


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
        raise CompanyRepositoryUnavailable from None
    sqlstate = _sqlstate(error)
    if sqlstate is not None and (sqlstate.startswith("08") or sqlstate in _TRANSIENT_SQLSTATES):
        raise CompanyRepositoryUnavailable from None


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


def _to_domain(record: CompanyRecord) -> Dimension[CompanyValue]:
    return Dimension(
        id=record.id,
        code=DimensionCode(record.code),
        value=CompanyValue(record.value),
        version=record.version,
        created_at=record.created_at,
        updated_at=record.updated_at,
        deleted_at=record.deleted_at,
    )
