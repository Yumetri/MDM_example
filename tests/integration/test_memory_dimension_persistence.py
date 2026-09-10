import asyncio
from collections.abc import AsyncGenerator
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from mdm.application.audit import HumanMutationAuditFactory
from mdm.application.auth import HumanPrincipal
from mdm.application.memory_dimensions import (
    MemoryDimensionCodeConflict,
    MemoryDimensionMultipleConflicts,
    MemoryDimensionValueConflict,
)
from mdm.application.preconditions import PreconditionFailed
from mdm.domain.audit import DimensionOperation
from mdm.domain.auth import UserRole
from mdm.domain.dimensions import DimensionCode, MemoryUnit, MemoryValue
from mdm.infrastructure.audit_context import SqlAlchemyMutationAuditContextWriter
from mdm.infrastructure.database import create_engine, create_session_factory
from mdm.infrastructure.repositories.memory_dimensions import SqlAlchemyMemoryRepository
from mdm.infrastructure.settings import Settings
from mdm.infrastructure.uuid7 import Uuid7Generator

ACTOR_ID = UUID("01890f7c-8abc-7def-8abc-abcdefabcdef")


@pytest.fixture
async def memory_engine() -> AsyncGenerator[AsyncEngine]:
    engine = create_engine(Settings().reveal_database_url())
    async with engine.begin() as connection:
        await connection.execute(text("TRUNCATE dimension_memory_logs, dimension_memories"))
    yield engine
    await engine.dispose()


def _audit(operation: DimensionOperation = DimensionOperation.CREATE):
    return HumanMutationAuditFactory(change_set_ids=Uuid7Generator().new).create(
        HumanPrincipal(user_id=ACTOR_ID, role=UserRole.ADMIN),
        dimension_operation=operation,
        reason="등록" if operation is DimensionOperation.CREATE else "수정",
    )


@pytest.mark.integration
async def test_memory_create_read_page_generated_capacity_and_audit(
    memory_engine: AsyncEngine,
) -> None:
    repository = SqlAlchemyMemoryRepository(create_session_factory(memory_engine))
    created = await repository.create(
        DimensionCode("MEM128"),
        MemoryValue(amount=128, unit=MemoryUnit.GB),
        _audit(),
    )
    fetched = await repository.get_active(created.id)
    page = await repository.list_active(after=None, limit=50)

    async with memory_engine.connect() as connection:
        stored = (
            (
                await connection.execute(
                    text(
                        "SELECT amount, unit, capacity_mb, is_generated, generation_expression "
                        "FROM dimension_memories "
                        "JOIN information_schema.columns ON table_name = 'dimension_memories' "
                        "AND column_name = 'capacity_mb' WHERE id = :id"
                    ),
                    {"id": created.id},
                )
            )
            .mappings()
            .one()
        )
        logs = (
            (
                await connection.execute(
                    text(
                        "SELECT field_name, new_value, change_set_id, changed_at "
                        "FROM dimension_memory_logs WHERE dimension_id = :id "
                        "ORDER BY field_name"
                    ),
                    {"id": created.id},
                )
            )
            .mappings()
            .all()
        )

    assert fetched == created
    assert page.items == (created,)
    assert page.has_more is False
    assert created.value.capacity_mb == 128_000
    assert stored["amount"] == 128
    assert stored["unit"] == "GB"
    assert stored["capacity_mb"] == 128_000
    assert stored["is_generated"] == "ALWAYS"
    assert stored["generation_expression"]
    assert [row["field_name"] for row in logs] == ["CODE", "VALUE"]
    assert logs[0]["new_value"] == "MEM128"
    assert logs[1]["new_value"] == {
        "amount": 128,
        "unit": "GB",
        "capacity_mb": 128_000,
    }
    assert logs[0]["change_set_id"] == logs[1]["change_set_id"]
    assert logs[0]["changed_at"] == logs[1]["changed_at"] == created.updated_at


@pytest.mark.integration
async def test_equivalent_capacity_and_code_conflicts_are_translated(
    memory_engine: AsyncEngine,
) -> None:
    repository = SqlAlchemyMemoryRepository(create_session_factory(memory_engine))
    await repository.create(
        DimensionCode("MEM1TB"), MemoryValue(amount=1, unit=MemoryUnit.TB), _audit()
    )

    with pytest.raises(MemoryDimensionValueConflict):
        await repository.create(
            DimensionCode("MEM1000GB"),
            MemoryValue(amount=1000, unit=MemoryUnit.GB),
            _audit(),
        )
    with pytest.raises(MemoryDimensionCodeConflict):
        await repository.create(
            DimensionCode("MEM1TB"), MemoryValue(amount=2, unit=MemoryUnit.TB), _audit()
        )
    with pytest.raises(MemoryDimensionMultipleConflicts):
        await repository.create(
            DimensionCode("MEM1TB"),
            MemoryValue(amount=1_000_000, unit=MemoryUnit.MB),
            _audit(),
        )


@pytest.mark.integration
async def test_concurrent_equivalent_memory_create_uses_database_unique_constraint(
    memory_engine: AsyncEngine,
) -> None:
    sessions = create_session_factory(memory_engine)
    results = await asyncio.gather(
        SqlAlchemyMemoryRepository(sessions).create(
            DimensionCode("MEM1TB"), MemoryValue(amount=1, unit=MemoryUnit.TB), _audit()
        ),
        SqlAlchemyMemoryRepository(sessions).create(
            DimensionCode("MEM1000GB"),
            MemoryValue(amount=1000, unit=MemoryUnit.GB),
            _audit(),
        ),
        return_exceptions=True,
    )

    assert sum(not isinstance(result, Exception) for result in results) == 1
    assert sum(isinstance(result, MemoryDimensionValueConflict) for result in results) == 1


@pytest.mark.integration
@pytest.mark.parametrize(
    ("columns", "values"),
    (
        ("code, amount, unit", "'BAD0', 0, 'GB'"),
        ("code, amount, unit", "'BADUNIT', 1, 'GIB'"),
        ("code, amount, unit, capacity_mb", "'FORGED', 1, 'GB', 1"),
    ),
)
async def test_database_rejects_invalid_or_client_supplied_memory_capacity(
    memory_engine: AsyncEngine,
    columns: str,
    values: str,
) -> None:
    sessions = create_session_factory(memory_engine)
    with pytest.raises(DBAPIError):
        async with sessions.begin() as session:
            await SqlAlchemyMutationAuditContextWriter(session).set_context(_audit())
            await session.execute(
                text(f"INSERT INTO dimension_memories ({columns}) VALUES ({values})")
            )

    async with memory_engine.connect() as connection:
        assert await connection.scalar(text("SELECT count(*) FROM dimension_memories")) == 0
        assert await connection.scalar(text("SELECT count(*) FROM dimension_memory_logs")) == 0


@pytest.mark.integration
async def test_memory_requires_context_and_logs_are_append_only(
    memory_engine: AsyncEngine,
) -> None:
    sessions = create_session_factory(memory_engine)
    repository = SqlAlchemyMemoryRepository(sessions)
    created = await repository.create(
        DimensionCode("MEM128"), MemoryValue(amount=128, unit=MemoryUnit.GB), _audit()
    )

    with pytest.raises(DBAPIError):
        async with sessions.begin() as session:
            await session.execute(
                text(
                    "INSERT INTO dimension_memories (code, amount, unit) "
                    "VALUES ('MEM256', 256, 'GB')"
                )
            )

    for statement in (
        "UPDATE dimension_memory_logs SET reason = '변조' WHERE dimension_id = :id",
        "DELETE FROM dimension_memory_logs WHERE dimension_id = :id",
        "DELETE FROM dimension_memories WHERE id = :id",
    ):
        with pytest.raises(DBAPIError):
            async with sessions.begin() as session:
                await session.execute(text(statement), {"id": created.id})


@pytest.mark.integration
@pytest.mark.parametrize(
    "invalid_json",
    (
        '{"amount": 1, "unit": "GB", "capacity_mb": 1}',
        '{"amount": 1, "unit": "GB", "capacity_mb": 1000, "extra": true}',
        '{"amount": 1.0, "unit": "GB", "capacity_mb": 1000}',
        '{"amount": 1, "unit": "GiB", "capacity_mb": 1000}',
        '{"amount": 1, "unit": "GB"}',
    ),
)
async def test_memory_log_rejects_invalid_value_shape(
    memory_engine: AsyncEngine,
    invalid_json: str,
) -> None:
    sessions = create_session_factory(memory_engine)
    repository = SqlAlchemyMemoryRepository(sessions)
    created = await repository.create(
        DimensionCode("MEM128"), MemoryValue(amount=128, unit=MemoryUnit.GB), _audit()
    )

    with pytest.raises(DBAPIError):
        async with sessions.begin() as session:
            await session.execute(
                text(
                    "INSERT INTO dimension_memory_logs ("
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


@pytest.mark.integration
async def test_memory_update_is_atomic_and_equivalent_capacity_is_noop(
    memory_engine: AsyncEngine,
) -> None:
    repository = SqlAlchemyMemoryRepository(create_session_factory(memory_engine))
    created = await repository.create(
        DimensionCode("MEM1TB"), MemoryValue(amount=1, unit=MemoryUnit.TB), _audit()
    )

    equivalent = await repository.update_value(
        created.id,
        1,
        MemoryValue(amount=1000, unit=MemoryUnit.GB),
        _audit(DimensionOperation.UPDATE),
    )
    updated = await repository.update_value(
        created.id,
        1,
        MemoryValue(amount=2, unit=MemoryUnit.TB),
        _audit(DimensionOperation.UPDATE),
    )
    with pytest.raises(PreconditionFailed):
        await repository.update_value(
            created.id,
            1,
            MemoryValue(amount=3, unit=MemoryUnit.TB),
            _audit(DimensionOperation.UPDATE),
        )

    async with memory_engine.connect() as connection:
        stored = (
            (
                await connection.execute(
                    text("SELECT amount, unit, capacity_mb FROM dimension_memories WHERE id = :id"),
                    {"id": created.id},
                )
            )
            .mappings()
            .one()
        )
        update_logs = (
            (
                await connection.execute(
                    text(
                        "SELECT old_value, new_value, dimension_version "
                        "FROM dimension_memory_logs "
                        "WHERE dimension_id = :id AND operation = 'UPDATE'"
                    ),
                    {"id": created.id},
                )
            )
            .mappings()
            .all()
        )

    assert equivalent == created
    assert updated.version == 2
    assert stored == {"amount": 2, "unit": "TB", "capacity_mb": 2_000_000}
    assert len(update_logs) == 1
    assert update_logs[0]["old_value"] == {
        "amount": 1,
        "unit": "TB",
        "capacity_mb": 1_000_000,
    }
    assert update_logs[0]["new_value"] == {
        "amount": 2,
        "unit": "TB",
        "capacity_mb": 2_000_000,
    }
    assert update_logs[0]["dimension_version"] == 2


@pytest.mark.integration
async def test_memory_update_reports_equivalent_capacity_conflict_and_rejects_sql_reexpression(
    memory_engine: AsyncEngine,
) -> None:
    sessions = create_session_factory(memory_engine)
    repository = SqlAlchemyMemoryRepository(sessions)
    first = await repository.create(
        DimensionCode("MEM1TB"), MemoryValue(amount=1, unit=MemoryUnit.TB), _audit()
    )
    await repository.create(
        DimensionCode("MEM2TB"), MemoryValue(amount=2, unit=MemoryUnit.TB), _audit()
    )

    with pytest.raises(MemoryDimensionValueConflict):
        await repository.update_value(
            first.id,
            1,
            MemoryValue(amount=2000, unit=MemoryUnit.GB),
            _audit(DimensionOperation.UPDATE),
        )

    with pytest.raises(DBAPIError):
        async with sessions.begin() as session:
            mutation_timestamp = await SqlAlchemyMutationAuditContextWriter(session).set_context(
                _audit(DimensionOperation.UPDATE), minimum_timestamp=first.updated_at
            )
            await session.execute(
                text(
                    "UPDATE dimension_memories "
                    "SET amount = 1000, unit = 'GB', version = version + 1, "
                    "updated_at = :updated_at WHERE id = :id"
                ),
                {"id": first.id, "updated_at": mutation_timestamp},
            )
