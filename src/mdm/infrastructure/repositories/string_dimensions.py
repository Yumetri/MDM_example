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
from mdm.application.master_codes import MasterCodeConflict
from mdm.application.preconditions import PreconditionFailed
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
from mdm.infrastructure.repositories.master_codes import (
    lock_referencing_master_codes,
    recompose_locked_master_codes,
)


class _SqlAlchemyStringDimensionRepository[
    ValueT: (ModelValue, BrandValue, CountryValue, CategoryValue)
]:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        dimension_name: str,
        table_name: str,
        slot: str,
        record_type: type[Any],
        value_type: Callable[[str], ValueT],
    ) -> None:
        self._session_factory = session_factory
        self._dimension_name = dimension_name
        self._table_name = table_name
        self._slot = slot
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

    async def update_value(
        self,
        dimension_id: UUID,
        expected_version: int,
        value: ValueT | None,
        audit: MutationAuditMetadata,
        *,
        code: DimensionCode | None = None,
    ) -> Dimension[ValueT]:
        try:
            async with self._session_factory.begin() as session:
                record = await session.scalar(
                    select(self._record_type)
                    .where(
                        self._record_type.id == dimension_id,
                        self._record_type.deleted_at.is_(None),
                    )
                    .with_for_update()
                )
                if record is None:
                    raise StringDimensionNotFound(self._dimension_name)
                current = self._to_domain(record)
                if current.version != expected_version:
                    raise PreconditionFailed
                code_changed = code is not None and code != current.code
                value_changed = value is not None and value != current.value
                if not code_changed and not value_changed:
                    return current

                conflict_conditions = []
                if code_changed:
                    assert code is not None
                    conflict_conditions.append(self._record_type.code == code.value)
                if value_changed:
                    assert value is not None
                    conflict_conditions.append(self._record_type.value == value.value)
                conflicts = (
                    await session.execute(
                        select(self._record_type.code, self._record_type.value).where(
                            self._record_type.id != dimension_id,
                            or_(*conflict_conditions),
                        )
                    )
                ).all()
                self._raise_conflict(
                    code_conflict=(
                        code_changed
                        and code is not None
                        and any(row.code == code.value for row in conflicts)
                    ),
                    value_conflict=(
                        value_changed
                        and value is not None
                        and any(row.value == value.value for row in conflicts)
                    ),
                )

                locked_master_codes = (
                    await lock_referencing_master_codes(
                        session,
                        slot=self._slot,
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
                        slot=self._slot,
                        dimension=changed,
                        changed_at=changed_at,
                    )
                    record.code = changed.code.value
                if value_changed:
                    record.value = changed.value.value
                record.version = changed.version
                record.updated_at = changed.updated_at
                await session.flush()
                return self._to_domain(record)
        except IntegrityError as error:
            constraint_name = _constraint_name(error)
            if constraint_name == f"uq_{self._table_name}_code":
                raise StringDimensionCodeConflict(self._dimension_name) from None
            if constraint_name == f"uq_{self._table_name}_value":
                raise StringDimensionValueConflict(self._dimension_name) from None
            if constraint_name in {"uq_master_codes_code", "uq_master_codes_references"}:
                raise MasterCodeConflict from None
            raise
        except MutationAuditContextUnavailable:
            raise StringDimensionRepositoryUnavailable from None
        except SQLAlchemyError as error:
            _raise_if_temporarily_unavailable(error)
            raise

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
            slot="model",
            record_type=ModelRecord,
            value_type=ModelValue,
        )


class SqlAlchemyBrandRepository(_SqlAlchemyStringDimensionRepository[BrandValue]):
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        super().__init__(
            session_factory,
            dimension_name="Brand",
            table_name="dimension_brands",
            slot="brand",
            record_type=BrandRecord,
            value_type=BrandValue,
        )


class SqlAlchemyCountryRepository(_SqlAlchemyStringDimensionRepository[CountryValue]):
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        super().__init__(
            session_factory,
            dimension_name="Country",
            table_name="dimension_countries",
            slot="country",
            record_type=CountryRecord,
            value_type=CountryValue,
        )


class SqlAlchemyCategoryRepository(_SqlAlchemyStringDimensionRepository[CategoryValue]):
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        super().__init__(
            session_factory,
            dimension_name="Category",
            table_name="dimension_categories",
            slot="category",
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
