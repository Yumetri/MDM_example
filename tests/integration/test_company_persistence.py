import asyncio
from collections.abc import AsyncGenerator
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from mdm.application.audit import HumanMutationAuditFactory, MutationAuditContextUnavailable
from mdm.application.auth import HumanPrincipal
from mdm.application.dimensions import (
    CompanyCodeConflict,
    CompanyCursor,
    CompanyMultipleConflicts,
    CompanyNotFound,
    CompanyRepositoryUnavailable,
    CompanyValueConflict,
)
from mdm.application.preconditions import PreconditionFailed
from mdm.domain.audit import DimensionOperation, MasterCodeOperation, MutationOperations
from mdm.domain.auth import UserRole
from mdm.domain.dimensions import CompanyValue, DimensionCode
from mdm.infrastructure.audit_context import SqlAlchemyMutationAuditContextWriter
from mdm.infrastructure.database import create_engine, create_session_factory
from mdm.infrastructure.repositories.companies import SqlAlchemyCompanyRepository
from mdm.infrastructure.settings import Settings
from mdm.infrastructure.uuid7 import Uuid7Generator

ACTOR_ID = UUID("01890f7c-8abc-7def-8abc-abcdefabcdef")


@pytest.fixture
async def company_engine() -> AsyncGenerator[AsyncEngine]:
    engine = create_engine(Settings().reveal_database_url())
    async with engine.begin() as connection:
        await connection.execute(text("TRUNCATE dimension_company_logs, dimension_companies"))
    yield engine
    await engine.dispose()


def _audit(operation: DimensionOperation = DimensionOperation.CREATE):
    return HumanMutationAuditFactory(change_set_ids=Uuid7Generator().new).create(
        HumanPrincipal(user_id=ACTOR_ID, role=UserRole.ADMIN),
        dimension_operation=operation,
        reason="최초 등록" if operation is DimensionOperation.CREATE else "값 수정",
    )


@pytest.mark.integration
async def test_create_persists_company_and_two_equal_context_audit_logs(
    company_engine: AsyncEngine,
) -> None:
    sessions = create_session_factory(company_engine)
    repository = SqlAlchemyCompanyRepository(sessions)

    company = await repository.create(
        DimensionCode("sam"), CompanyValue("Samsung Electronics"), _audit()
    )

    async with company_engine.connect() as connection:
        rows = (
            (
                await connection.execute(
                    text(
                        """
                        SELECT field_name, old_value, new_value, change_set_id,
                               dimension_version, operation, actor_kind, actor_role,
                               actor_id, reason, changed_at
                        FROM dimension_company_logs
                        WHERE dimension_id = :dimension_id
                        ORDER BY field_name
                        """
                    ),
                    {"dimension_id": company.id},
                )
            )
            .mappings()
            .all()
        )

    assert company.code.value == "SAM"
    assert company.value.value == "SAMSUNG_ELECTRONICS"
    assert company.version == 1
    assert company.created_at == company.updated_at
    assert company.deleted_at is None
    assert [row["field_name"] for row in rows] == ["CODE", "VALUE"]
    assert rows[0]["old_value"] is None
    assert rows[0]["new_value"] == "SAM"
    assert rows[1]["new_value"] == "SAMSUNG_ELECTRONICS"
    assert rows[0]["change_set_id"] == rows[1]["change_set_id"]
    assert rows[0]["changed_at"] == rows[1]["changed_at"] == company.updated_at
    assert all(row["dimension_version"] == 1 for row in rows)
    assert all(row["operation"] == "CREATE" for row in rows)
    assert all(row["actor_kind"] == "HUMAN" for row in rows)
    assert all(row["actor_role"] == "ADMIN" for row in rows)
    assert all(row["actor_id"] == str(ACTOR_ID) for row in rows)
    assert all(row["reason"] == "최초 등록" for row in rows)


@pytest.mark.integration
async def test_direct_write_without_context_and_log_mutation_are_rejected(
    company_engine: AsyncEngine,
) -> None:
    sessions = create_session_factory(company_engine)
    repository = SqlAlchemyCompanyRepository(sessions)
    company = await repository.create(DimensionCode("SAM"), CompanyValue("Samsung"), _audit())

    with pytest.raises(DBAPIError):
        async with sessions.begin() as session:
            await session.execute(
                text("INSERT INTO dimension_companies (code, value) VALUES ('APP', 'APPLE')")
            )

    with pytest.raises(DBAPIError):
        async with sessions.begin() as session:
            await SqlAlchemyMutationAuditContextWriter(session).set_context(_audit())
            await session.execute(
                text("INSERT INTO dimension_companies (code, value) VALUES ('NNN', 'INVALID')")
            )

    for statement in (
        "UPDATE dimension_company_logs SET reason = '변조' WHERE dimension_id = :id",
        "DELETE FROM dimension_company_logs WHERE dimension_id = :id",
    ):
        with pytest.raises(DBAPIError):
            async with sessions.begin() as session:
                await session.execute(text(statement), {"id": company.id})


@pytest.mark.integration
async def test_company_insert_rejects_context_without_dimension_operation(
    company_engine: AsyncEngine,
) -> None:
    sessions = create_session_factory(company_engine)
    audit = _audit()
    master_code_only_audit = type(audit)(
        change_set_id=audit.change_set_id,
        actor=audit.actor,
        operations=MutationOperations(master_code=MasterCodeOperation.CREATE),
        reason=audit.reason,
    )

    with pytest.raises(DBAPIError):
        async with sessions.begin() as session:
            await SqlAlchemyMutationAuditContextWriter(session).set_context(master_code_only_audit)
            await session.execute(
                text("INSERT INTO dimension_companies (code, value) VALUES ('APP', 'APPLE')")
            )


@pytest.mark.integration
@pytest.mark.parametrize(
    ("operation", "old_value", "new_value"),
    [("DELETE", None, "true"), ("RESTORE", "true", None)],
)
async def test_deletion_log_rejects_sql_null_transition_values(
    company_engine: AsyncEngine,
    operation: str,
    old_value: str | None,
    new_value: str | None,
) -> None:
    sessions = create_session_factory(company_engine)
    repository = SqlAlchemyCompanyRepository(sessions)
    company = await repository.create(DimensionCode("SAM"), CompanyValue("Samsung"), _audit())

    with pytest.raises(DBAPIError):
        async with sessions.begin() as session:
            await session.execute(
                text(
                    """
                    INSERT INTO dimension_company_logs (
                        dimension_id, change_set_id, dimension_version, operation, field_name,
                        old_value, new_value, actor_kind, actor_role, actor_id, changed_at
                    ) VALUES (
                        :dimension_id, uuidv7(), 2, :operation, 'DELETED',
                        CAST(:old_value AS jsonb), CAST(:new_value AS jsonb),
                        'HUMAN', 'ADMIN', :actor_id, statement_timestamp()
                    )
                    """
                ),
                {
                    "dimension_id": company.id,
                    "operation": operation,
                    "old_value": old_value,
                    "new_value": new_value,
                    "actor_id": str(ACTOR_ID),
                },
            )


@pytest.mark.integration
async def test_audit_context_failure_is_translated_without_diagnostics(
    company_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail_context(*args, **kwargs):
        del args, kwargs
        raise MutationAuditContextUnavailable("database detail")

    monkeypatch.setattr(SqlAlchemyMutationAuditContextWriter, "set_context", fail_context)
    repository = SqlAlchemyCompanyRepository(create_session_factory(company_engine))

    with pytest.raises(CompanyRepositoryUnavailable) as captured:
        await repository.create(DimensionCode("SAM"), CompanyValue("Samsung"), _audit())

    assert str(captured.value) == ""


@pytest.mark.integration
async def test_create_reports_code_value_and_combined_conflicts(
    company_engine: AsyncEngine,
) -> None:
    repository = SqlAlchemyCompanyRepository(create_session_factory(company_engine))
    await repository.create(DimensionCode("SAM"), CompanyValue("Samsung"), _audit())
    await repository.create(DimensionCode("APP"), CompanyValue("Apple"), _audit())

    with pytest.raises(CompanyCodeConflict):
        await repository.create(DimensionCode("SAM"), CompanyValue("Google"), _audit())
    with pytest.raises(CompanyValueConflict):
        await repository.create(DimensionCode("GOO"), CompanyValue("Samsung"), _audit())
    with pytest.raises(CompanyMultipleConflicts):
        await repository.create(DimensionCode("SAM"), CompanyValue("Apple"), _audit())


@pytest.mark.integration
async def test_active_get_and_descending_cursor_pages_have_no_duplicates(
    company_engine: AsyncEngine,
) -> None:
    repository = SqlAlchemyCompanyRepository(create_session_factory(company_engine))
    created = [
        await repository.create(DimensionCode(code), CompanyValue(value), _audit())
        for code, value in (("A", "Alpha"), ("B", "Beta"), ("C", "Charlie"))
    ]

    first = await repository.list_active(after=None, limit=2)
    cursor = CompanyCursor(created_at=first.items[-1].created_at, id=first.items[-1].id)
    second = await repository.list_active(after=cursor, limit=2)

    assert first.has_more is True
    assert second.has_more is False
    assert [item.id for item in (*first.items, *second.items)] == [
        item.id for item in reversed(created)
    ]
    assert await repository.get_active(created[0].id) == created[0]
    with pytest.raises(CompanyNotFound):
        await repository.get_active(UUID("01890f7c-8abc-7def-8abc-000000000000"))


@pytest.mark.integration
async def test_concurrent_create_relies_on_unique_constraint_for_final_integrity(
    company_engine: AsyncEngine,
) -> None:
    sessions = create_session_factory(company_engine)
    first_repository = SqlAlchemyCompanyRepository(sessions)
    second_repository = SqlAlchemyCompanyRepository(sessions)

    results = await asyncio.gather(
        first_repository.create(DimensionCode("SAM"), CompanyValue("Samsung"), _audit()),
        second_repository.create(DimensionCode("SAM"), CompanyValue("Samsung2"), _audit()),
        return_exceptions=True,
    )

    assert sum(not isinstance(result, Exception) for result in results) == 1
    assert sum(isinstance(result, CompanyCodeConflict) for result in results) == 1

    async with company_engine.connect() as connection:
        count = await connection.scalar(
            text("SELECT count(*) FROM dimension_companies WHERE code = 'SAM'")
        )
    assert count == 1


@pytest.mark.integration
async def test_company_value_update_locks_increments_once_and_writes_one_value_log(
    company_engine: AsyncEngine,
) -> None:
    repository = SqlAlchemyCompanyRepository(create_session_factory(company_engine))
    created = await repository.create(DimensionCode("SAM"), CompanyValue("Samsung"), _audit())

    updated = await repository.update_value(
        created.id,
        1,
        CompanyValue("Apple Korea"),
        _audit(DimensionOperation.UPDATE),
    )

    async with company_engine.connect() as connection:
        logs = (
            (
                await connection.execute(
                    text(
                        "SELECT operation, field_name, old_value, new_value, reason, "
                        "dimension_version, changed_at FROM dimension_company_logs "
                        "WHERE dimension_id = :id ORDER BY dimension_version, field_name"
                    ),
                    {"id": created.id},
                )
            )
            .mappings()
            .all()
        )

    assert updated.code == created.code
    assert updated.value == CompanyValue("APPLE_KOREA")
    assert updated.version == 2
    assert updated.updated_at >= created.updated_at
    assert [(row["operation"], row["field_name"]) for row in logs] == [
        ("CREATE", "CODE"),
        ("CREATE", "VALUE"),
        ("UPDATE", "VALUE"),
    ]
    assert logs[-1]["old_value"] == "SAMSUNG"
    assert logs[-1]["new_value"] == "APPLE_KOREA"
    assert logs[-1]["reason"] == "값 수정"
    assert logs[-1]["dimension_version"] == 2
    assert logs[-1]["changed_at"] == updated.updated_at


@pytest.mark.integration
async def test_company_value_noop_and_stale_precondition_do_not_change_state_or_logs(
    company_engine: AsyncEngine,
) -> None:
    repository = SqlAlchemyCompanyRepository(create_session_factory(company_engine))
    created = await repository.create(DimensionCode("SAM"), CompanyValue("Samsung"), _audit())

    noop = await repository.update_value(
        created.id,
        1,
        CompanyValue(" samsung "),
        _audit(DimensionOperation.UPDATE),
    )
    with pytest.raises(PreconditionFailed):
        await repository.update_value(
            created.id,
            9,
            CompanyValue("Apple"),
            _audit(DimensionOperation.UPDATE),
        )

    async with company_engine.connect() as connection:
        log_count = await connection.scalar(
            text("SELECT count(*) FROM dimension_company_logs WHERE dimension_id = :id"),
            {"id": created.id},
        )
    assert noop == created
    assert log_count == 2


@pytest.mark.integration
async def test_company_update_reports_value_conflict_and_database_guards_transition_shape(
    company_engine: AsyncEngine,
) -> None:
    sessions = create_session_factory(company_engine)
    repository = SqlAlchemyCompanyRepository(sessions)
    first = await repository.create(DimensionCode("SAM"), CompanyValue("Samsung"), _audit())
    await repository.create(DimensionCode("APP"), CompanyValue("Apple"), _audit())

    with pytest.raises(CompanyValueConflict):
        await repository.update_value(
            first.id,
            1,
            CompanyValue("Apple"),
            _audit(DimensionOperation.UPDATE),
        )

    with pytest.raises(DBAPIError):
        async with sessions.begin() as session:
            changed_at = await SqlAlchemyMutationAuditContextWriter(session).set_context(
                _audit(DimensionOperation.UPDATE), minimum_timestamp=first.updated_at
            )
            await session.execute(
                text(
                    "UPDATE dimension_companies SET code = 'GOO', version = version + 1, "
                    "updated_at = :changed_at WHERE id = :id"
                ),
                {"changed_at": changed_at, "id": first.id},
            )

    with pytest.raises(DBAPIError):
        async with sessions.begin() as session:
            await SqlAlchemyMutationAuditContextWriter(session).set_context(
                _audit(DimensionOperation.UPDATE), minimum_timestamp=first.updated_at
            )
            await session.execute(
                text("UPDATE dimension_companies SET value = 'GOOGLE' WHERE id = :id"),
                {"id": first.id},
            )


@pytest.mark.integration
async def test_concurrent_company_updates_with_one_etag_commit_only_once(
    company_engine: AsyncEngine,
) -> None:
    sessions = create_session_factory(company_engine)
    created = await SqlAlchemyCompanyRepository(sessions).create(
        DimensionCode("SAM"), CompanyValue("Samsung"), _audit()
    )

    results = await asyncio.gather(
        SqlAlchemyCompanyRepository(sessions).update_value(
            created.id,
            1,
            CompanyValue("Apple"),
            _audit(DimensionOperation.UPDATE),
        ),
        SqlAlchemyCompanyRepository(sessions).update_value(
            created.id,
            1,
            CompanyValue("Google"),
            _audit(DimensionOperation.UPDATE),
        ),
        return_exceptions=True,
    )

    assert sum(not isinstance(result, Exception) for result in results) == 1
    assert sum(isinstance(result, PreconditionFailed) for result in results) == 1
