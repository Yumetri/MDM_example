"""Async SQLAlchemy MasterCode repository with ordered Dimension resolution."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import Select, and_, or_, select
from sqlalchemy.exc import (
    DisconnectionError,
    IntegrityError,
    OperationalError,
    SQLAlchemyError,
)
from sqlalchemy.exc import TimeoutError as SQLAlchemyTimeoutError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mdm.application.audit import MutationAuditContextUnavailable
from mdm.application.master_codes import (
    ExistingDimension,
    InlineDimension,
    InlineDimensionConflict,
    InvalidDimensionReference,
    MasterCodeConflict,
    MasterCodeCreatePlan,
    MasterCodeCursor,
    MasterCodeNotFound,
    MasterCodePage,
    MasterCodeRepositoryUnavailable,
    NotApplicable,
)
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
from mdm.domain.master_codes import MasterCode, MasterCodeDimension, MasterCodeDimensions
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


@dataclass(frozen=True, slots=True)
class _SlotConfig:
    record_type: type[Any]
    to_domain: Callable[[Any], MasterCodeDimension]
    value_column: str
    value_for_query: Callable[[Any], Any]
    record_values: Callable[[Any], dict[str, object]]


def _string_dimension(record_type: type[Any], value_type: type[Any]) -> _SlotConfig:
    def to_domain(record: Any) -> MasterCodeDimension:
        return _common_dimension(record, value_type(record.value))

    return _SlotConfig(
        record_type=record_type,
        to_domain=to_domain,
        value_column="value",
        value_for_query=lambda value: value.value,
        record_values=lambda value: {"value": value.value},
    )


def _common_dimension(record: Any, value: Any) -> Any:
    return Dimension(
        id=record.id,
        code=DimensionCode(record.code),
        value=value,
        version=record.version,
        created_at=record.created_at,
        updated_at=record.updated_at,
        deleted_at=record.deleted_at,
    )


_SLOTS: dict[str, _SlotConfig] = {
    "company": _string_dimension(CompanyRecord, CompanyValue),
    "brand": _string_dimension(BrandRecord, BrandValue),
    "model": _string_dimension(ModelRecord, ModelValue),
    "category": _string_dimension(CategoryRecord, CategoryValue),
    "year": _SlotConfig(
        record_type=YearRecord,
        to_domain=lambda record: _common_dimension(record, YearValue(record.value)),
        value_column="value",
        value_for_query=lambda value: value.value,
        record_values=lambda value: {"value": value.value},
    ),
    "memory": _SlotConfig(
        record_type=MemoryRecord,
        to_domain=lambda record: _common_dimension(
            record,
            MemoryValue(amount=record.amount, unit=MemoryUnit(record.unit)),
        ),
        value_column="capacity_mb",
        value_for_query=lambda value: value.capacity_mb,
        record_values=lambda value: {"amount": value.amount, "unit": value.unit.value},
    ),
    "network": _SlotConfig(
        record_type=NetworkRecord,
        to_domain=lambda record: _common_dimension(record, NetworkGeneration(record.value)),
        value_column="value",
        value_for_query=lambda value: value.value,
        record_values=lambda value: {"value": value.value},
    ),
    "country": _string_dimension(CountryRecord, CountryValue),
}


class SqlAlchemyMasterCodeRepository:
    """Persist one MasterCode and all inline Dimensions in a single transaction."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def create(
        self,
        plan: MasterCodeCreatePlan,
        audit: MutationAuditMetadata,
    ) -> MasterCode:
        try:
            async with self._session_factory.begin() as session:
                mutation_timestamp = await SqlAlchemyMutationAuditContextWriter(
                    session
                ).set_context(audit)
                resolved: dict[str, MasterCodeDimension | None] = {}
                invalid: list[str] = []
                for slot in MasterCodeDimensions.ORDER:
                    selection = getattr(plan, slot)
                    if isinstance(selection, NotApplicable):
                        resolved[slot] = None
                    elif isinstance(selection, ExistingDimension):
                        dimension = await self._lock_reference(session, slot, selection.id)
                        if dimension is None:
                            invalid.append(f"dimensions.{slot}.id")
                        else:
                            resolved[slot] = dimension
                    else:
                        assert isinstance(selection, InlineDimension)
                        resolved[slot] = await self._create_inline(
                            session,
                            slot,
                            selection,
                            mutation_timestamp=mutation_timestamp,
                        )
                if invalid:
                    raise InvalidDimensionReference(tuple(invalid))

                dimensions = MasterCodeDimensions(**resolved)
                record = MasterCodeRecord(
                    **{
                        f"{slot}_id": (None if dimension is None else dimension.id)
                        for slot, dimension in resolved.items()
                    },
                    code=MasterCode.compose(dimensions),
                    created_at=mutation_timestamp,
                    updated_at=mutation_timestamp,
                )
                session.add(record)
                await session.flush()
                return _master_code(record, dimensions)
        except IntegrityError as error:
            await self._translate_integrity_error(error, plan)
            raise
        except MutationAuditContextUnavailable:
            raise MasterCodeRepositoryUnavailable from None
        except SQLAlchemyError as error:
            _raise_if_temporarily_unavailable(error)
            raise

    async def get_active(self, master_code_id: UUID) -> MasterCode:
        statement = _joined_statement().where(
            MasterCodeRecord.id == master_code_id,
            MasterCodeRecord.deleted_at.is_(None),
        )
        try:
            async with self._session_factory() as session:
                row = (await session.execute(statement)).one_or_none()
        except SQLAlchemyError as error:
            _raise_if_temporarily_unavailable(error)
            raise
        if row is None:
            raise MasterCodeNotFound
        return _joined_to_domain(row)

    async def list_active(
        self,
        *,
        after: MasterCodeCursor | None,
        limit: int,
    ) -> MasterCodePage:
        statement = _joined_statement().where(MasterCodeRecord.deleted_at.is_(None))
        if after is not None:
            statement = statement.where(
                or_(
                    MasterCodeRecord.created_at < after.created_at,
                    and_(
                        MasterCodeRecord.created_at == after.created_at,
                        MasterCodeRecord.id < after.id,
                    ),
                )
            )
        statement = statement.order_by(
            MasterCodeRecord.created_at.desc(), MasterCodeRecord.id.desc()
        ).limit(limit + 1)
        try:
            async with self._session_factory() as session:
                rows = (await session.execute(statement)).all()
        except SQLAlchemyError as error:
            _raise_if_temporarily_unavailable(error)
            raise
        return MasterCodePage(
            items=tuple(_joined_to_domain(row) for row in rows[:limit]),
            has_more=len(rows) > limit,
        )

    async def _lock_reference(
        self,
        session: AsyncSession,
        slot: str,
        dimension_id: UUID,
    ) -> MasterCodeDimension | None:
        config = _SLOTS[slot]
        record = await session.scalar(
            select(config.record_type)
            .where(
                config.record_type.id == dimension_id,
                config.record_type.deleted_at.is_(None),
            )
            .with_for_update(read=True)
        )
        return None if record is None else config.to_domain(record)

    async def _create_inline(
        self,
        session: AsyncSession,
        slot: str,
        selection: InlineDimension,
        *,
        mutation_timestamp: Any,
    ) -> MasterCodeDimension:
        config = _SLOTS[slot]
        assert isinstance(selection.code, DimensionCode)
        code_conflict, value_conflict = await self._find_inline_conflicts(
            session, config, selection
        )
        if code_conflict or value_conflict:
            raise InlineDimensionConflict(slot, code=code_conflict, value=value_conflict)

        record = config.record_type(
            code=selection.code.value,
            **config.record_values(selection.value),
            created_at=mutation_timestamp,
            updated_at=mutation_timestamp,
        )
        session.add(record)
        await session.flush()
        return config.to_domain(record)

    @staticmethod
    async def _find_inline_conflicts(
        session: AsyncSession,
        config: _SlotConfig,
        selection: InlineDimension,
    ) -> tuple[bool, bool]:
        assert isinstance(selection.code, DimensionCode)
        value_column = getattr(config.record_type, config.value_column)
        expected_value = config.value_for_query(selection.value)
        conflicts = (
            await session.execute(
                select(config.record_type.code, value_column).where(
                    or_(
                        config.record_type.code == selection.code.value,
                        value_column == expected_value,
                    )
                )
            )
        ).all()
        return (
            any(row[0] == selection.code.value for row in conflicts),
            any(row[1] == expected_value for row in conflicts),
        )

    async def _translate_integrity_error(
        self,
        error: IntegrityError,
        plan: MasterCodeCreatePlan,
    ) -> None:
        constraint = _constraint_name(error)
        if constraint in {"uq_master_codes_code", "uq_master_codes_references"}:
            raise MasterCodeConflict from None
        for slot, config in _SLOTS.items():
            table = config.record_type.__tablename__
            code_constraint = f"uq_{table}_code"
            expected_value_constraint = (
                f"uq_{table}_capacity_mb" if slot == "memory" else f"uq_{table}_value"
            )
            if constraint not in {code_constraint, expected_value_constraint}:
                continue

            selection = getattr(plan, slot)
            if isinstance(selection, InlineDimension):
                try:
                    async with self._session_factory() as session:
                        code_conflict, value_conflict = await self._find_inline_conflicts(
                            session, config, selection
                        )
                except SQLAlchemyError as requery_error:
                    _raise_if_temporarily_unavailable(requery_error)
                    raise
                if code_conflict or value_conflict:
                    raise InlineDimensionConflict(
                        slot,
                        code=code_conflict,
                        value=value_conflict,
                    ) from None

            raise InlineDimensionConflict(
                slot,
                code=constraint == code_constraint,
                value=constraint == expected_value_constraint,
            ) from None


def _joined_statement() -> Select[Any]:
    return (
        select(
            MasterCodeRecord,
            CompanyRecord,
            BrandRecord,
            ModelRecord,
            CategoryRecord,
            YearRecord,
            MemoryRecord,
            NetworkRecord,
            CountryRecord,
        )
        .outerjoin(CompanyRecord, MasterCodeRecord.company_id == CompanyRecord.id)
        .outerjoin(BrandRecord, MasterCodeRecord.brand_id == BrandRecord.id)
        .outerjoin(ModelRecord, MasterCodeRecord.model_id == ModelRecord.id)
        .outerjoin(CategoryRecord, MasterCodeRecord.category_id == CategoryRecord.id)
        .outerjoin(YearRecord, MasterCodeRecord.year_id == YearRecord.id)
        .outerjoin(MemoryRecord, MasterCodeRecord.memory_id == MemoryRecord.id)
        .outerjoin(NetworkRecord, MasterCodeRecord.network_id == NetworkRecord.id)
        .outerjoin(CountryRecord, MasterCodeRecord.country_id == CountryRecord.id)
    )


def _joined_to_domain(row: Any) -> MasterCode:
    record = row[0]
    dimensions = MasterCodeDimensions(
        **{
            slot: None if dimension_record is None else _SLOTS[slot].to_domain(dimension_record)
            for slot, dimension_record in zip(
                MasterCodeDimensions.ORDER,
                row[1:],
                strict=True,
            )
        }
    )
    return _master_code(record, dimensions)


def _master_code(record: MasterCodeRecord, dimensions: MasterCodeDimensions) -> MasterCode:
    return MasterCode(
        id=record.id,
        dimensions=dimensions,
        code=record.code,
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
        raise MasterCodeRepositoryUnavailable from None
    sqlstate = _sqlstate(error)
    if sqlstate is not None and (sqlstate.startswith("08") or sqlstate in _TRANSIENT_SQLSTATES):
        raise MasterCodeRepositoryUnavailable from None


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
