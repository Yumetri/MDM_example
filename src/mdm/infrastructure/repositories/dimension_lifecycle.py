"""Transactional lifecycle adapters for the eight independent Dimension tables."""

from collections.abc import Callable
from typing import Any
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mdm.application.audit import MutationAuditContextUnavailable
from mdm.application.dimension_lifecycle import (
    BrandLifecycleRepository,
    CategoryLifecycleRepository,
    CompanyLifecycleRepository,
    CountryLifecycleRepository,
    DimensionInUse,
    DimensionLifecycleNotFound,
    DimensionLifecycleUnavailable,
    DimensionNotDeleted,
    MemoryLifecycleRepository,
    ModelLifecycleRepository,
    NetworkLifecycleRepository,
    YearLifecycleRepository,
)
from mdm.application.dimensions import CompanyRepositoryUnavailable
from mdm.application.preconditions import PreconditionFailed
from mdm.domain.audit import MutationAuditMetadata
from mdm.domain.dimensions import (
    BrandValue,
    CategoryValue,
    CompanyValue,
    CountryValue,
    Dimension,
    DimensionCode,
    MemoryUnit,
    MemoryValue,
    ModelValue,
    NetworkGeneration,
    YearValue,
)
from mdm.infrastructure.audit_context import SqlAlchemyMutationAuditContextWriter
from mdm.infrastructure.models import (
    BrandRecord,
    CategoryRecord,
    CompanyRecord,
    CountryRecord,
    MasterCodeRecord,
    MemoryRecord,
    ModelRecord,
    NetworkRecord,
    YearRecord,
)
from mdm.infrastructure.repositories.companies import _raise_if_temporarily_unavailable


class _SqlAlchemyDimensionLifecycleRepository[ValueT]:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        record_type: type[Any],
        slot: str,
        value_from_record: Callable[[Any], ValueT],
    ) -> None:
        self._session_factory = session_factory
        self._record_type = record_type
        self._slot = slot
        self._value_from_record = value_from_record

    def _to_domain(self, record: Any) -> Dimension[ValueT]:
        return Dimension(
            id=record.id,
            code=DimensionCode(record.code),
            value=self._value_from_record(record),
            version=record.version,
            created_at=record.created_at,
            updated_at=record.updated_at,
            deleted_at=record.deleted_at,
        )

    async def get_tombstone(self, dimension_id: UUID) -> Dimension[ValueT]:
        try:
            async with self._session_factory() as session:
                record = await session.scalar(
                    select(self._record_type).where(self._record_type.id == dimension_id)
                )
                if record is None:
                    raise DimensionLifecycleNotFound
                if record.deleted_at is None:
                    raise DimensionNotDeleted
                return self._to_domain(record)
        except SQLAlchemyError as error:
            _translate_unavailable(error)
            raise

    async def delete(
        self, dimension_id: UUID, expected_version: int, audit: MutationAuditMetadata
    ) -> Dimension[ValueT]:
        return await self._mutate(dimension_id, expected_version, audit, deleting=True)

    async def restore(
        self, dimension_id: UUID, expected_version: int, audit: MutationAuditMetadata
    ) -> Dimension[ValueT]:
        return await self._mutate(dimension_id, expected_version, audit, deleting=False)

    async def _mutate(
        self,
        dimension_id: UUID,
        expected_version: int,
        audit: MutationAuditMetadata,
        *,
        deleting: bool,
    ) -> Dimension[ValueT]:
        record_type = self._record_type
        try:
            async with self._session_factory.begin() as session:
                record = await session.scalar(
                    select(record_type)
                    .where(record_type.id == dimension_id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
                if record is None or (deleting and record.deleted_at is not None):
                    raise DimensionLifecycleNotFound
                if not deleting and record.deleted_at is None:
                    raise DimensionNotDeleted
                current = self._to_domain(record)
                if current.version != expected_version:
                    raise PreconditionFailed
                # All MasterCode constructors lock their non-null Dimensions first.
                # Holding this Dimension prevents a new active reference from racing this query.
                if deleting:
                    referenced = await session.scalar(
                        select(MasterCodeRecord.id)
                        .where(
                            getattr(MasterCodeRecord, f"{self._slot}_id") == dimension_id,
                            MasterCodeRecord.deleted_at.is_(None),
                        )
                        .limit(1)
                    )
                    if referenced is not None:
                        raise DimensionInUse
                changed_at = await SqlAlchemyMutationAuditContextWriter(session).set_context(
                    audit, minimum_timestamp=current.updated_at
                )
                changed = (
                    current.delete(changed_at=changed_at)
                    if deleting
                    else current.restore(changed_at=changed_at)
                )
                result = await session.scalar(
                    update(record_type)
                    .where(record_type.id == dimension_id, record_type.version == expected_version)
                    .values(
                        version=changed.version,
                        updated_at=changed.updated_at,
                        deleted_at=changed.deleted_at,
                    )
                    .returning(record_type)
                    .execution_options(populate_existing=True)
                )
                if result is None:
                    raise PreconditionFailed
                return self._to_domain(result)
        except MutationAuditContextUnavailable:
            raise DimensionLifecycleUnavailable from None
        except SQLAlchemyError as error:
            _translate_unavailable(error)
            raise


def _translate_unavailable(error: SQLAlchemyError) -> None:
    try:
        _raise_if_temporarily_unavailable(error)
    except CompanyRepositoryUnavailable:
        raise DimensionLifecycleUnavailable from None


class SqlAlchemyCompanyLifecycleRepository(
    _SqlAlchemyDimensionLifecycleRepository[CompanyValue], CompanyLifecycleRepository
):
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        super().__init__(
            session_factory,
            record_type=CompanyRecord,
            slot="company",
            value_from_record=lambda row: CompanyValue(row.value),
        )


class SqlAlchemyBrandLifecycleRepository(
    _SqlAlchemyDimensionLifecycleRepository[BrandValue], BrandLifecycleRepository
):
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        super().__init__(
            session_factory,
            record_type=BrandRecord,
            slot="brand",
            value_from_record=lambda row: BrandValue(row.value),
        )


class SqlAlchemyModelLifecycleRepository(
    _SqlAlchemyDimensionLifecycleRepository[ModelValue], ModelLifecycleRepository
):
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        super().__init__(
            session_factory,
            record_type=ModelRecord,
            slot="model",
            value_from_record=lambda row: ModelValue(row.value),
        )


class SqlAlchemyCategoryLifecycleRepository(
    _SqlAlchemyDimensionLifecycleRepository[CategoryValue], CategoryLifecycleRepository
):
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        super().__init__(
            session_factory,
            record_type=CategoryRecord,
            slot="category",
            value_from_record=lambda row: CategoryValue(row.value),
        )


class SqlAlchemyCountryLifecycleRepository(
    _SqlAlchemyDimensionLifecycleRepository[CountryValue], CountryLifecycleRepository
):
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        super().__init__(
            session_factory,
            record_type=CountryRecord,
            slot="country",
            value_from_record=lambda row: CountryValue(row.value),
        )


class SqlAlchemyYearLifecycleRepository(
    _SqlAlchemyDimensionLifecycleRepository[YearValue], YearLifecycleRepository
):
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        super().__init__(
            session_factory,
            record_type=YearRecord,
            slot="year",
            value_from_record=lambda row: YearValue(row.value),
        )


class SqlAlchemyNetworkLifecycleRepository(
    _SqlAlchemyDimensionLifecycleRepository[NetworkGeneration], NetworkLifecycleRepository
):
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        super().__init__(
            session_factory,
            record_type=NetworkRecord,
            slot="network",
            value_from_record=lambda row: NetworkGeneration(row.value),
        )


class SqlAlchemyMemoryLifecycleRepository(
    _SqlAlchemyDimensionLifecycleRepository[MemoryValue], MemoryLifecycleRepository
):
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        super().__init__(
            session_factory,
            record_type=MemoryRecord,
            slot="memory",
            value_from_record=lambda row: MemoryValue(row.amount, MemoryUnit(row.unit)),
        )
