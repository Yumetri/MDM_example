"""Async SQLAlchemy repositories for Model, Brand, Country, and Category."""

from collections.abc import Callable
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
from mdm.application.string_dimensions import (
    StringDimensionCodeConflict,
    StringDimensionCursor,
    StringDimensionMultipleConflicts,
    StringDimensionNotFound,
    StringDimensionPage,
    StringDimensionRepositoryUnavailable,
    StringDimensionValueConflict,
)
from mdm.domain.audit import MutationAuditMetadata
from mdm.domain.dimensions import (
    BrandValue,
    CategoryValue,
    CountryValue,
    Dimension,
    DimensionCode,
    ModelValue,
)
from mdm.infrastructure.audit_context import SqlAlchemyMutationAuditContextWriter
from mdm.infrastructure.models import BrandRecord, CategoryRecord, CountryRecord, ModelRecord


class _SqlAlchemyStringDimensionRepository[
    ValueT: (ModelValue, BrandValue, CountryValue, CategoryValue)
]:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        dimension_name: str,
        table_name: str,
        record_type: type[Any],
        value_type: Callable[[str], ValueT],
    ) -> None:
        self._session_factory = session_factory
        self._dimension_name = dimension_name
        self._table_name = table_name
        self._record_type = record_type
        self._value_type = value_type

    async def create(
        self,
        code: DimensionCode,
        value: ValueT,
        audit: MutationAuditMetadata,
    ) -> Dimension[ValueT]:
        try:
            async with self._session_factory.begin() as session:
                conflicts = (
                    await session.execute(
                        select(self._record_type.code, self._record_type.value).where(
                            or_(
                                self._record_type.code == code.value,
                                self._record_type.value == value.value,
                            )
                        )
                    )
                ).all()
                code_conflict = any(row.code == code.value for row in conflicts)
                value_conflict = any(row.value == value.value for row in conflicts)
                self._raise_conflict(
                    code_conflict=code_conflict,
                    value_conflict=value_conflict,
                )

                await SqlAlchemyMutationAuditContextWriter(session).set_context(audit)
                record = self._record_type(code=code.value, value=value.value)
                session.add(record)
                await session.flush()
                return self._to_domain(record)
        except IntegrityError as error:
            constraint_name = _constraint_name(error)
            if constraint_name == f"uq_{self._table_name}_code":
                raise StringDimensionCodeConflict(self._dimension_name) from None
            if constraint_name == f"uq_{self._table_name}_value":
                raise StringDimensionValueConflict(self._dimension_name) from None
            raise
        except MutationAuditContextUnavailable:
            raise StringDimensionRepositoryUnavailable from None
        except SQLAlchemyError as error:
            _raise_if_temporarily_unavailable(error)
            raise

    async def get_active(self, dimension_id: UUID) -> Dimension[ValueT]:
        try:
            async with self._session_factory() as session:
                record = await session.scalar(
                    select(self._record_type).where(
                        self._record_type.id == dimension_id,
                        self._record_type.deleted_at.is_(None),
                    )
                )
        except SQLAlchemyError as error:
            _raise_if_temporarily_unavailable(error)
            raise
        if record is None:
            raise StringDimensionNotFound(self._dimension_name)
        return self._to_domain(record)

    async def list_active(
        self,
        *,
        after: StringDimensionCursor | None,
        limit: int,
    ) -> StringDimensionPage[ValueT]:
        statement = select(self._record_type).where(self._record_type.deleted_at.is_(None))
        if after is not None:
            statement = statement.where(
                or_(
                    self._record_type.created_at < after.created_at,
                    and_(
                        self._record_type.created_at == after.created_at,
                        self._record_type.id < after.id,
                    ),
                )
            )
        statement = statement.order_by(
            self._record_type.created_at.desc(), self._record_type.id.desc()
        ).limit(limit + 1)
        try:
            async with self._session_factory() as session:
                records = (await session.scalars(statement)).all()
        except SQLAlchemyError as error:
            _raise_if_temporarily_unavailable(error)
            raise
        return StringDimensionPage(
            items=tuple(self._to_domain(record) for record in records[:limit]),
            has_more=len(records) > limit,
        )

    def _raise_conflict(self, *, code_conflict: bool, value_conflict: bool) -> None:
        if code_conflict and value_conflict:
            raise StringDimensionMultipleConflicts(self._dimension_name)
        if code_conflict:
            raise StringDimensionCodeConflict(self._dimension_name)
        if value_conflict:
            raise StringDimensionValueConflict(self._dimension_name)

    def _to_domain(self, record: Any) -> Dimension[ValueT]:
        return Dimension(
            id=record.id,
            code=DimensionCode(record.code),
            value=self._value_type(record.value),
            version=record.version,
            created_at=record.created_at,
            updated_at=record.updated_at,
            deleted_at=record.deleted_at,
        )


class SqlAlchemyModelRepository(_SqlAlchemyStringDimensionRepository[ModelValue]):
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        super().__init__(
            session_factory,
            dimension_name="Model",
            table_name="dimension_models",
            record_type=ModelRecord,
            value_type=ModelValue,
        )


class SqlAlchemyBrandRepository(_SqlAlchemyStringDimensionRepository[BrandValue]):
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        super().__init__(
            session_factory,
            dimension_name="Brand",
            table_name="dimension_brands",
            record_type=BrandRecord,
            value_type=BrandValue,
        )


class SqlAlchemyCountryRepository(_SqlAlchemyStringDimensionRepository[CountryValue]):
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        super().__init__(
            session_factory,
            dimension_name="Country",
            table_name="dimension_countries",
            record_type=CountryRecord,
            value_type=CountryValue,
        )


class SqlAlchemyCategoryRepository(_SqlAlchemyStringDimensionRepository[CategoryValue]):
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        super().__init__(
            session_factory,
            dimension_name="Category",
            table_name="dimension_categories",
            record_type=CategoryRecord,
            value_type=CategoryValue,
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
        raise StringDimensionRepositoryUnavailable from None
    sqlstate = _sqlstate(error)
    if sqlstate is not None and (sqlstate.startswith("08") or sqlstate in _TRANSIENT_SQLSTATES):
        raise StringDimensionRepositoryUnavailable from None


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
