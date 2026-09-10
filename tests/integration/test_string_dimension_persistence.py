import asyncio
from collections.abc import AsyncGenerator
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from mdm.application.audit import HumanMutationAuditFactory
from mdm.application.auth import HumanPrincipal
from mdm.application.preconditions import PreconditionFailed
from mdm.application.string_dimensions import (
    StringDimensionCodeConflict,
    StringDimensionMultipleConflicts,
    StringDimensionValueConflict,
)
from mdm.domain.audit import DimensionOperation
from mdm.domain.auth import UserRole
from mdm.domain.dimensions import (
    BrandValue,
    CategoryValue,
    CountryValue,
    DimensionCode,
    ModelValue,
)
from mdm.infrastructure.database import create_engine, create_session_factory
from mdm.infrastructure.repositories.string_dimensions import (
    SqlAlchemyBrandRepository,
    SqlAlchemyCategoryRepository,
    SqlAlchemyCountryRepository,
    SqlAlchemyModelRepository,
)
from mdm.infrastructure.settings import Settings
from mdm.infrastructure.uuid7 import Uuid7Generator

ACTOR_ID = UUID("01890f7c-8abc-7def-8abc-abcdefabcdef")

CASES = (
    (SqlAlchemyModelRepository, ModelValue, "dimension_models", "dimension_model_logs"),
    (SqlAlchemyBrandRepository, BrandValue, "dimension_brands", "dimension_brand_logs"),
    (SqlAlchemyCountryRepository, CountryValue, "dimension_countries", "dimension_country_logs"),
    (
        SqlAlchemyCategoryRepository,
        CategoryValue,
        "dimension_categories",
        "dimension_category_logs",
    ),
)


@pytest.fixture
async def string_dimension_engine() -> AsyncGenerator[AsyncEngine]:
    engine = create_engine(Settings().reveal_database_url())
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "TRUNCATE master_code_logs, master_codes, "
                "dimension_model_logs, dimension_models, "
                "dimension_brand_logs, dimension_brands, "
                "dimension_country_logs, dimension_countries, "
                "dimension_category_logs, dimension_categories"
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
@pytest.mark.parametrize(("repository_type", "value_type", "table", "log_table"), CASES)
async def test_string_dimension_create_read_page_and_audit(
    string_dimension_engine: AsyncEngine,
    repository_type: type,
    value_type: type,
    table: str,
    log_table: str,
) -> None:
    repository = repository_type(create_session_factory(string_dimension_engine))
    created = await repository.create(DimensionCode("VALUE1"), value_type("Galaxy S24"), _audit())
    fetched = await repository.get_active(created.id)
    page = await repository.list_active(after=None, limit=50)

    async with string_dimension_engine.connect() as connection:
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

    assert fetched == created
    assert page.items == (created,)
    assert page.has_more is False
    assert created.value.value == "GALAXY_S24"
    assert [row["field_name"] for row in rows] == ["CODE", "VALUE"]
    assert [row["new_value"] for row in rows] == ["VALUE1", "GALAXY_S24"]
    assert rows[0]["change_set_id"] == rows[1]["change_set_id"]
    assert rows[0]["changed_at"] == rows[1]["changed_at"] == created.updated_at

    count = await connection_count(string_dimension_engine, table)
    assert count == 1


@pytest.mark.integration
@pytest.mark.parametrize(("repository_type", "value_type", "_table", "_log_table"), CASES)
async def test_string_dimension_unique_code_is_translated(
    string_dimension_engine: AsyncEngine,
    repository_type: type,
    value_type: type,
    _table: str,
    _log_table: str,
) -> None:
    repository = repository_type(create_session_factory(string_dimension_engine))
    await repository.create(DimensionCode("VALUE1"), value_type("First"), _audit())

    with pytest.raises(StringDimensionCodeConflict):
        await repository.create(DimensionCode("VALUE1"), value_type("Second"), _audit())

    await repository.create(DimensionCode("VALUE2"), value_type("Third"), _audit())
    with pytest.raises(StringDimensionValueConflict):
        await repository.create(DimensionCode("VALUE3"), value_type("First"), _audit())
    with pytest.raises(StringDimensionMultipleConflicts):
        await repository.create(DimensionCode("VALUE1"), value_type("Third"), _audit())


@pytest.mark.integration
@pytest.mark.parametrize(("repository_type", "value_type", "table", "log_table"), CASES)
async def test_string_dimension_requires_context_and_logs_are_append_only(
    string_dimension_engine: AsyncEngine,
    repository_type: type,
    value_type: type,
    table: str,
    log_table: str,
) -> None:
    sessions = create_session_factory(string_dimension_engine)
    repository = repository_type(sessions)
    created = await repository.create(DimensionCode("VALUE1"), value_type("First"), _audit())

    with pytest.raises(DBAPIError):
        async with sessions.begin() as session:
            await session.execute(
                text(f"INSERT INTO {table} (code, value) VALUES ('VALUE2', 'SECOND')")
            )

    for statement in (
        f"UPDATE {log_table} SET reason = '변조' WHERE dimension_id = :id",
        f"DELETE FROM {log_table} WHERE dimension_id = :id",
    ):
        with pytest.raises(DBAPIError):
            async with sessions.begin() as session:
                await session.execute(text(statement), {"id": created.id})


@pytest.mark.integration
@pytest.mark.parametrize(("repository_type", "value_type", "_table", "_log_table"), CASES)
async def test_concurrent_string_dimension_create_uses_database_unique_constraint(
    string_dimension_engine: AsyncEngine,
    repository_type: type,
    value_type: type,
    _table: str,
    _log_table: str,
) -> None:
    sessions = create_session_factory(string_dimension_engine)
    first_repository = repository_type(sessions)
    second_repository = repository_type(sessions)

    results = await asyncio.gather(
        first_repository.create(DimensionCode("VALUE1"), value_type("First"), _audit()),
        second_repository.create(DimensionCode("VALUE1"), value_type("Second"), _audit()),
        return_exceptions=True,
    )

    assert sum(not isinstance(result, Exception) for result in results) == 1
    assert sum(isinstance(result, StringDimensionCodeConflict) for result in results) == 1


@pytest.mark.integration
@pytest.mark.parametrize(("repository_type", "value_type", "_table", "log_table"), CASES)
async def test_each_string_dimension_updates_value_and_logs_exactly_once(
    string_dimension_engine: AsyncEngine,
    repository_type: type,
    value_type: type,
    _table: str,
    log_table: str,
) -> None:
    repository = repository_type(create_session_factory(string_dimension_engine))
    created = await repository.create(DimensionCode("VALUE1"), value_type("First"), _audit())

    updated = await repository.update_value(
        created.id,
        1,
        value_type(" Second Value "),
        _audit(DimensionOperation.UPDATE),
    )
    noop = await repository.update_value(
        created.id,
        2,
        value_type("second__value"),
        _audit(DimensionOperation.UPDATE),
    )
    with pytest.raises(PreconditionFailed):
        await repository.update_value(
            created.id,
            1,
            value_type("Third"),
            _audit(DimensionOperation.UPDATE),
        )

    async with string_dimension_engine.connect() as connection:
        logs = (
            (
                await connection.execute(
                    text(
                        f"SELECT operation, field_name, old_value, new_value "
                        f"FROM {log_table} WHERE dimension_id = :id "
                        "ORDER BY dimension_version, field_name"
                    ),
                    {"id": created.id},
                )
            )
            .mappings()
            .all()
        )

    assert updated.code == created.code
    assert updated.value == value_type("SECOND_VALUE")
    assert updated.version == 2
    assert noop == updated
    assert [(row["operation"], row["field_name"]) for row in logs] == [
        ("CREATE", "CODE"),
        ("CREATE", "VALUE"),
        ("UPDATE", "VALUE"),
    ]
    assert logs[-1]["old_value"] == "FIRST"
    assert logs[-1]["new_value"] == "SECOND_VALUE"


@pytest.mark.integration
@pytest.mark.parametrize(("repository_type", "value_type", "_table", "_log_table"), CASES)
async def test_each_string_dimension_update_reports_value_conflict(
    string_dimension_engine: AsyncEngine,
    repository_type: type,
    value_type: type,
    _table: str,
    _log_table: str,
) -> None:
    repository = repository_type(create_session_factory(string_dimension_engine))
    first = await repository.create(DimensionCode("VALUE1"), value_type("First"), _audit())
    await repository.create(DimensionCode("VALUE2"), value_type("Second"), _audit())

    with pytest.raises(StringDimensionValueConflict):
        await repository.update_value(
            first.id,
            1,
            value_type("Second"),
            _audit(DimensionOperation.UPDATE),
        )


async def connection_count(engine: AsyncEngine, table: str) -> int:
    async with engine.connect() as connection:
        value = await connection.scalar(text(f"SELECT count(*) FROM {table}"))
    assert isinstance(value, int)
    return value
