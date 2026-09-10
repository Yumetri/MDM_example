import asyncio
from collections.abc import AsyncGenerator
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from mdm.application.audit import HumanMutationAuditFactory
from mdm.application.auth import HumanPrincipal
from mdm.application.numeric_dimensions import (
    NumericDimensionCodeConflict,
    NumericDimensionMultipleConflicts,
    NumericDimensionValueConflict,
)
from mdm.application.preconditions import PreconditionFailed
from mdm.domain.audit import DimensionOperation
from mdm.domain.auth import UserRole
from mdm.domain.dimensions import DimensionCode, NetworkGeneration, YearValue
from mdm.infrastructure.audit_context import SqlAlchemyMutationAuditContextWriter
from mdm.infrastructure.database import create_engine, create_session_factory
from mdm.infrastructure.repositories.numeric_dimensions import (
    SqlAlchemyNetworkRepository,
    SqlAlchemyYearRepository,
)
from mdm.infrastructure.settings import Settings
from mdm.infrastructure.uuid7 import Uuid7Generator

ACTOR_ID = UUID("01890f7c-8abc-7def-8abc-abcdefabcdef")

CASES = (
    (
        SqlAlchemyYearRepository,
        YearValue,
        2026,
        2027,
        "dimension_years",
        "dimension_year_logs",
    ),
    (
        SqlAlchemyNetworkRepository,
        NetworkGeneration,
        5,
        4,
        "dimension_networks",
        "dimension_network_logs",
    ),
)


@pytest.fixture
async def numeric_dimension_engine() -> AsyncGenerator[AsyncEngine]:
    engine = create_engine(Settings().reveal_database_url())
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "TRUNCATE dimension_year_logs, dimension_years, "
                "dimension_network_logs, dimension_networks"
            )
        )
    yield engine
    await engine.dispose()


def _audit(operation: DimensionOperation = DimensionOperation.CREATE):
    return HumanMutationAuditFactory(change_set_ids=Uuid7Generator().new).create(
        HumanPrincipal(user_id=ACTOR_ID, role=UserRole.ADMIN),
        dimension_operation=operation,
        reason="등록" if operation is DimensionOperation.CREATE else "수정",
    )


@pytest.mark.integration
@pytest.mark.parametrize(
    ("repository_type", "value_type", "raw", "_other", "table", "log_table"), CASES
)
async def test_numeric_dimension_create_read_page_and_audit(
    numeric_dimension_engine: AsyncEngine,
    repository_type: type,
    value_type: type,
    raw: int,
    _other: int,
    table: str,
    log_table: str,
) -> None:
    repository = repository_type(create_session_factory(numeric_dimension_engine))
    created = await repository.create(DimensionCode("VALUE1"), value_type(raw), _audit())
    fetched = await repository.get_active(created.id)
    page = await repository.list_active(after=None, limit=50)

    async with numeric_dimension_engine.connect() as connection:
        rows = (
            (
                await connection.execute(
                    text(
                        f"SELECT field_name, new_value, change_set_id, changed_at "
                        f"FROM {log_table} WHERE dimension_id = :dimension_id ORDER BY field_name"
                    ),
                    {"dimension_id": created.id},
                )
            )
            .mappings()
            .all()
        )
        data_type = await connection.scalar(
            text(
                "SELECT data_type FROM information_schema.columns "
                "WHERE table_name = :table AND column_name = 'value'"
            ),
            {"table": table},
        )

    assert fetched == created
    assert page.items == (created,)
    assert page.has_more is False
    assert created.value.value == raw
    assert [row["field_name"] for row in rows] == ["CODE", "VALUE"]
    assert [row["new_value"] for row in rows] == ["VALUE1", raw]
    assert rows[0]["change_set_id"] == rows[1]["change_set_id"]
    assert rows[0]["changed_at"] == rows[1]["changed_at"] == created.updated_at
    assert data_type == "smallint"


@pytest.mark.integration
@pytest.mark.parametrize(
    ("repository_type", "value_type", "raw", "other", "_table", "_log_table"), CASES
)
async def test_numeric_dimension_unique_conflicts_are_translated(
    numeric_dimension_engine: AsyncEngine,
    repository_type: type,
    value_type: type,
    raw: int,
    other: int,
    _table: str,
    _log_table: str,
) -> None:
    repository = repository_type(create_session_factory(numeric_dimension_engine))
    await repository.create(DimensionCode("VALUE1"), value_type(raw), _audit())

    with pytest.raises(NumericDimensionCodeConflict):
        await repository.create(DimensionCode("VALUE1"), value_type(other), _audit())

    await repository.create(DimensionCode("VALUE2"), value_type(other), _audit())
    with pytest.raises(NumericDimensionValueConflict):
        await repository.create(DimensionCode("VALUE3"), value_type(raw), _audit())
    with pytest.raises(NumericDimensionMultipleConflicts):
        await repository.create(DimensionCode("VALUE1"), value_type(other), _audit())


@pytest.mark.integration
@pytest.mark.parametrize(
    ("repository_type", "value_type", "raw", "_other", "table", "log_table"), CASES
)
async def test_numeric_dimension_requires_context_and_logs_are_append_only(
    numeric_dimension_engine: AsyncEngine,
    repository_type: type,
    value_type: type,
    raw: int,
    _other: int,
    table: str,
    log_table: str,
) -> None:
    sessions = create_session_factory(numeric_dimension_engine)
    repository = repository_type(sessions)
    created = await repository.create(DimensionCode("VALUE1"), value_type(raw), _audit())

    with pytest.raises(DBAPIError):
        async with sessions.begin() as session:
            await session.execute(
                text(f"INSERT INTO {table} (code, value) VALUES ('VALUE2', :value)"),
                {"value": raw},
            )

    for statement in (
        f"UPDATE {log_table} SET reason = '변조' WHERE dimension_id = :id",
        f"DELETE FROM {log_table} WHERE dimension_id = :id",
    ):
        with pytest.raises(DBAPIError):
            async with sessions.begin() as session:
                await session.execute(text(statement), {"id": created.id})


@pytest.mark.integration
@pytest.mark.parametrize(
    ("repository_type", "value_type", "raw", "other", "_table", "_log_table"), CASES
)
async def test_concurrent_numeric_dimension_create_uses_database_unique_constraint(
    numeric_dimension_engine: AsyncEngine,
    repository_type: type,
    value_type: type,
    raw: int,
    other: int,
    _table: str,
    _log_table: str,
) -> None:
    sessions = create_session_factory(numeric_dimension_engine)
    first_repository = repository_type(sessions)
    second_repository = repository_type(sessions)

    results = await asyncio.gather(
        first_repository.create(DimensionCode("VALUE1"), value_type(raw), _audit()),
        second_repository.create(DimensionCode("VALUE1"), value_type(other), _audit()),
        return_exceptions=True,
    )

    assert sum(not isinstance(result, Exception) for result in results) == 1
    assert sum(isinstance(result, NumericDimensionCodeConflict) for result in results) == 1


@pytest.mark.integration
@pytest.mark.parametrize(
    ("repository_type", "value_type", "raw", "other", "_table", "log_table"), CASES
)
async def test_each_numeric_dimension_updates_value_and_honors_precondition(
    numeric_dimension_engine: AsyncEngine,
    repository_type: type,
    value_type: type,
    raw: int,
    other: int,
    _table: str,
    log_table: str,
) -> None:
    repository = repository_type(create_session_factory(numeric_dimension_engine))
    created = await repository.create(DimensionCode("VALUE1"), value_type(raw), _audit())

    updated = await repository.update_value(
        created.id,
        1,
        value_type(other),
        _audit(DimensionOperation.UPDATE),
    )
    noop = await repository.update_value(
        created.id,
        2,
        value_type(other),
        _audit(DimensionOperation.UPDATE),
    )
    with pytest.raises(PreconditionFailed):
        await repository.update_value(
            created.id,
            1,
            value_type(raw),
            _audit(DimensionOperation.UPDATE),
        )

    async with numeric_dimension_engine.connect() as connection:
        update_log = (
            (
                await connection.execute(
                    text(
                        f"SELECT old_value, new_value, dimension_version FROM {log_table} "
                        "WHERE dimension_id = :id AND operation = 'UPDATE'"
                    ),
                    {"id": created.id},
                )
            )
            .mappings()
            .one()
        )

    assert updated.code == created.code
    assert updated.value == value_type(other)
    assert updated.version == 2
    assert noop == updated
    assert update_log["old_value"] == raw
    assert update_log["new_value"] == other
    assert update_log["dimension_version"] == 2


@pytest.mark.integration
@pytest.mark.parametrize(
    ("repository_type", "value_type", "raw", "other", "_table", "_log_table"), CASES
)
async def test_each_numeric_dimension_update_reports_value_conflict(
    numeric_dimension_engine: AsyncEngine,
    repository_type: type,
    value_type: type,
    raw: int,
    other: int,
    _table: str,
    _log_table: str,
) -> None:
    repository = repository_type(create_session_factory(numeric_dimension_engine))
    first = await repository.create(DimensionCode("VALUE1"), value_type(raw), _audit())
    await repository.create(DimensionCode("VALUE2"), value_type(other), _audit())

    with pytest.raises(NumericDimensionValueConflict):
        await repository.update_value(
            first.id,
            1,
            value_type(other),
            _audit(DimensionOperation.UPDATE),
        )


@pytest.mark.integration
@pytest.mark.parametrize(
    ("table", "log_table", "invalid_value"),
    (
        ("dimension_years", "dimension_year_logs", 1999),
        ("dimension_years", "dimension_year_logs", 3000),
        ("dimension_networks", "dimension_network_logs", 0),
        ("dimension_networks", "dimension_network_logs", 6),
    ),
)
async def test_numeric_dimension_database_rejects_out_of_range_values(
    numeric_dimension_engine: AsyncEngine,
    table: str,
    log_table: str,
    invalid_value: int,
) -> None:
    sessions = create_session_factory(numeric_dimension_engine)
    audit = _audit()
    with pytest.raises(DBAPIError):
        async with sessions.begin() as session:
            await SqlAlchemyMutationAuditContextWriter(session).set_context(audit)
            await session.execute(
                text(f"INSERT INTO {table} (code, value) VALUES ('VALUE1', :value)"),
                {"value": invalid_value},
            )

    async with numeric_dimension_engine.connect() as connection:
        assert await connection.scalar(text(f"SELECT count(*) FROM {table}")) == 0
        assert await connection.scalar(text(f"SELECT count(*) FROM {log_table}")) == 0


@pytest.mark.integration
@pytest.mark.parametrize(
    ("repository_type", "value_type", "raw", "log_table", "invalid_json"),
    (
        (SqlAlchemyYearRepository, YearValue, 2026, "dimension_year_logs", '"2026"'),
        (SqlAlchemyYearRepository, YearValue, 2026, "dimension_year_logs", "2026.0"),
        (SqlAlchemyNetworkRepository, NetworkGeneration, 5, "dimension_network_logs", "true"),
        (SqlAlchemyNetworkRepository, NetworkGeneration, 5, "dimension_network_logs", "5.0"),
    ),
)
async def test_numeric_dimension_log_rejects_non_integer_json_values(
    numeric_dimension_engine: AsyncEngine,
    repository_type: type,
    value_type: type,
    raw: int,
    log_table: str,
    invalid_json: str,
) -> None:
    sessions = create_session_factory(numeric_dimension_engine)
    repository = repository_type(sessions)
    created = await repository.create(DimensionCode("VALUE1"), value_type(raw), _audit())

    with pytest.raises(DBAPIError):
        async with sessions.begin() as session:
            await session.execute(
                text(
                    f"INSERT INTO {log_table} ("
                    "dimension_id, change_set_id, dimension_version, operation, field_name, "
                    "old_value, new_value, reason, actor_kind, actor_role, actor_id, changed_at"
                    ") VALUES ("
                    ":dimension_id, :change_set_id, 1, 'CREATE', 'VALUE', "
                    "NULL, CAST(:new_value AS jsonb), NULL, 'HUMAN', 'ADMIN', :actor_id, now()"
                    ")"
                ),
                {
                    "dimension_id": created.id,
                    "change_set_id": Uuid7Generator().new(),
                    "new_value": invalid_json,
                    "actor_id": str(ACTOR_ID),
                },
            )
