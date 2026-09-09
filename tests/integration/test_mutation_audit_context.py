from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from mdm.domain.audit import (
    ActorKind,
    AuditActor,
    DimensionOperation,
    MasterCodeOperation,
    MutationAuditMetadata,
    MutationOperations,
)
from mdm.domain.auth import UserRole
from mdm.infrastructure.audit_context import SqlAlchemyMutationAuditContextWriter
from mdm.infrastructure.database import create_engine, create_session_factory
from mdm.infrastructure.settings import Settings

CHANGE_SET_ID = UUID("01890f7c-8abc-7def-8abc-0123456789ab")
USER_ID = UUID("01890f7c-8abc-7def-8abc-abcdefabcdef")


@pytest.fixture
async def audit_engine() -> AsyncGenerator[AsyncEngine]:
    engine = create_engine(Settings().reveal_database_url())
    yield engine
    await engine.dispose()


def _metadata(
    *,
    actor: AuditActor | None = None,
    dimension_operation: DimensionOperation | None = DimensionOperation.UPDATE,
    master_code_operation: MasterCodeOperation | None = MasterCodeOperation.RECOMPOSE,
) -> MutationAuditMetadata:
    return MutationAuditMetadata(
        change_set_id=CHANGE_SET_ID,
        actor=actor or AuditActor.human(user_id=USER_ID, role=UserRole.ADMIN),
        operations=MutationOperations(
            dimension=dimension_operation,
            master_code=master_code_operation,
        ),
        reason="코드 변경",
    )


async def _install_write_probe(session: AsyncSession) -> None:
    await session.execute(
        text("CREATE TEMP TABLE mutation_audit_probe (id INTEGER) ON COMMIT DROP")
    )
    await session.execute(
        text(
            """
            CREATE FUNCTION pg_temp.require_mutation_audit_context()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $function$
            BEGIN
                PERFORM * FROM mdm_current_mutation_audit_context();
                RETURN NEW;
            END
            $function$
            """
        )
    )
    await session.execute(
        text(
            """
            CREATE TRIGGER mutation_audit_probe_context
            BEFORE INSERT ON mutation_audit_probe
            FOR EACH ROW EXECUTE FUNCTION pg_temp.require_mutation_audit_context()
            """
        )
    )


@pytest.mark.integration
async def test_context_writer_sets_one_validated_transaction_local_context(
    audit_engine: AsyncEngine,
) -> None:
    sessions = create_session_factory(audit_engine)
    minimum_timestamp = datetime.now(UTC) + timedelta(seconds=1)

    async with sessions.begin() as session:
        mutation_timestamp = await SqlAlchemyMutationAuditContextWriter(session).set_context(
            _metadata(), minimum_timestamp=minimum_timestamp
        )
        row = (
            (await session.execute(text("SELECT * FROM mdm_current_mutation_audit_context()")))
            .mappings()
            .one()
        )
        await _install_write_probe(session)
        await session.execute(text("INSERT INTO mutation_audit_probe (id) VALUES (1)"))

    assert mutation_timestamp >= minimum_timestamp
    assert row == {
        "change_set_id": CHANGE_SET_ID,
        "actor_kind": "HUMAN",
        "actor_role": "ADMIN",
        "actor_id": str(USER_ID),
        "reason": "코드 변경",
        "dimension_operation": "UPDATE",
        "master_code_operation": "RECOMPOSE",
        "mutation_timestamp": mutation_timestamp,
    }


@pytest.mark.integration
async def test_database_context_can_represent_future_system_shape(
    audit_engine: AsyncEngine,
) -> None:
    sessions = create_session_factory(audit_engine)
    metadata = _metadata(
        actor=AuditActor(kind=ActorKind.SYSTEM, actor_id="nightly-recompose", role=None)
    )

    async with sessions.begin() as session:
        await SqlAlchemyMutationAuditContextWriter(session).set_context(metadata)
        row = (
            (await session.execute(text("SELECT * FROM mdm_current_mutation_audit_context()")))
            .mappings()
            .one()
        )

    assert row["actor_kind"] == "SYSTEM"
    assert row["actor_role"] is None
    assert row["actor_id"] == "nightly-recompose"


@pytest.mark.integration
async def test_write_probe_rejects_missing_context(audit_engine: AsyncEngine) -> None:
    sessions = create_session_factory(audit_engine)

    with pytest.raises(DBAPIError):
        async with sessions.begin() as session:
            await _install_write_probe(session)
            await session.execute(text("INSERT INTO mutation_audit_probe (id) VALUES (1)"))


@pytest.mark.integration
@pytest.mark.parametrize(
    ("setting", "invalid_value"),
    [
        ("mdm.actor_kind", "ROBOT"),
        ("mdm.actor_kind", ""),
        ("mdm.actor_kind", "SYSTEM"),
        ("mdm.actor_role", ""),
        ("mdm.actor_id", ""),
        ("mdm.change_set_id", "12345678-1234-4123-8123-123456789abc"),
        ("mdm.change_set_id", "01890f7c-8abc-7def-0abc-0123456789ab"),
        ("mdm.change_set_id", ""),
        ("mdm.dimension_operation", "UPSERT"),
        ("mdm.reason", "탭\t포함"),
        ("mdm.mutation_timestamp", "not-a-timestamp"),
        ("mdm.mutation_timestamp", ""),
    ],
)
async def test_write_probe_rejects_malformed_context(
    audit_engine: AsyncEngine,
    setting: str,
    invalid_value: str,
) -> None:
    sessions = create_session_factory(audit_engine)

    with pytest.raises(DBAPIError):
        async with sessions.begin() as session:
            await SqlAlchemyMutationAuditContextWriter(session).set_context(_metadata())
            await session.execute(
                text("SELECT set_config(:setting, :value, true)"),
                {"setting": setting, "value": invalid_value},
            )
            await _install_write_probe(session)
            await session.execute(text("INSERT INTO mutation_audit_probe (id) VALUES (1)"))


@pytest.mark.integration
async def test_write_probe_rejects_context_without_any_entity_operation(
    audit_engine: AsyncEngine,
) -> None:
    sessions = create_session_factory(audit_engine)

    with pytest.raises(DBAPIError):
        async with sessions.begin() as session:
            await SqlAlchemyMutationAuditContextWriter(session).set_context(_metadata())
            for setting in ("mdm.dimension_operation", "mdm.master_code_operation"):
                await session.execute(
                    text("SELECT set_config(:setting, '', true)"),
                    {"setting": setting},
                )
            await _install_write_probe(session)
            await session.execute(text("INSERT INTO mutation_audit_probe (id) VALUES (1)"))


@pytest.mark.integration
@pytest.mark.parametrize("outcome", ["commit", "rollback", "failure"])
async def test_context_does_not_leak_when_pool_connection_is_reused(
    audit_engine: AsyncEngine,
    outcome: str,
) -> None:
    sessions = create_session_factory(audit_engine)
    first_session = sessions()
    try:
        await first_session.begin()
        first_pid = await first_session.scalar(text("SELECT pg_backend_pid()"))
        await SqlAlchemyMutationAuditContextWriter(first_session).set_context(_metadata())
        if outcome == "commit":
            await first_session.commit()
        elif outcome == "rollback":
            await first_session.rollback()
        else:
            with pytest.raises(DBAPIError):
                await first_session.execute(text("SELECT 1 / 0"))
            await first_session.rollback()
    finally:
        await first_session.close()

    async with sessions.begin() as second_session:
        second_pid = await second_session.scalar(text("SELECT pg_backend_pid()"))
        leaked_values = (
            (
                await second_session.execute(
                    text(
                        """
                    SELECT
                        NULLIF(current_setting('mdm.actor_kind', true), '') AS actor_kind,
                        NULLIF(current_setting('mdm.change_set_id', true), '') AS change_set_id,
                        NULLIF(current_setting('mdm.mutation_timestamp', true), '')
                            AS mutation_timestamp
                    """
                    )
                )
            )
            .mappings()
            .one()
        )

    assert second_pid == first_pid
    assert leaked_values == {
        "actor_kind": None,
        "change_set_id": None,
        "mutation_timestamp": None,
    }
