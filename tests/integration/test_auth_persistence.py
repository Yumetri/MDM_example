import asyncio
import traceback
from collections.abc import AsyncGenerator

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from mdm.application.users import (
    DuplicateUserEmail,
    NewUser,
    NewUserSecurityEvent,
)
from mdm.domain.auth import (
    DisplayName,
    EmailAddress,
    SecurityEventInitiatorType,
    SecurityEventType,
    UserRole,
    UserStatus,
)
from mdm.infrastructure.database import create_engine, create_session_factory
from mdm.infrastructure.repositories.users import (
    SqlAlchemyUserRepository,
    SqlAlchemyUserSecurityEventRepository,
)
from mdm.infrastructure.settings import Settings


@pytest.fixture
async def auth_engine() -> AsyncGenerator[AsyncEngine]:
    engine = create_engine(Settings().reveal_database_url())
    async with engine.begin() as connection:
        await connection.execute(text("TRUNCATE user_security_events, users"))
    yield engine
    await engine.dispose()


def _new_user(email: str = "admin@example.net") -> NewUser:
    return NewUser(
        email=EmailAddress(email),
        name=DisplayName("관리자"),
        password_hash="$argon2id$v=19$m=19456,t=2,p=1$test$hash",
        role=UserRole.SUPER_ADMIN,
        status=UserStatus.ACTIVE,
    )


@pytest.mark.integration
async def test_database_engine_hides_secret_query_parameters(
    auth_engine: AsyncEngine,
) -> None:
    assert auth_engine.sync_engine.hide_parameters is True


@pytest.mark.integration
async def test_user_repository_round_trip_uses_uuidv7_and_normalized_email(
    auth_engine: AsyncEngine,
) -> None:
    sessions = create_session_factory(auth_engine)
    async with sessions.begin() as session:
        repository = SqlAlchemyUserRepository(session)
        created = await repository.add(_new_user("  ADMIN@Example.net "))

    async with sessions() as session:
        found = await SqlAlchemyUserRepository(session).get_by_email(
            EmailAddress("admin@example.net")
        )

    assert created.id.version == 7
    assert found == created
    assert created.email.value == "admin@example.net"
    assert created.created_at.tzinfo is not None
    assert created.updated_at >= created.created_at


@pytest.mark.integration
async def test_database_unique_email_serializes_concurrent_writes(
    auth_engine: AsyncEngine,
) -> None:
    sessions = create_session_factory(auth_engine)
    first_session = sessions()
    second_session = sessions()
    try:
        first = SqlAlchemyUserRepository(first_session)
        second = SqlAlchemyUserRepository(second_session)
        await first.add(_new_user("duplicate@example.net"))

        second_write = asyncio.create_task(second.add(_new_user("DUPLICATE@example.net")))
        await asyncio.sleep(0.05)
        assert not second_write.done()

        await first_session.commit()
        with pytest.raises(DuplicateUserEmail) as duplicate:
            await second_write
        assert _new_user().password_hash not in "".join(traceback.format_exception(duplicate.value))
        await second_session.rollback()
    finally:
        await first_session.close()
        await second_session.close()


@pytest.mark.integration
async def test_database_rejects_invalid_role_and_oversized_email(
    auth_engine: AsyncEngine,
) -> None:
    with pytest.raises(IntegrityError):
        async with auth_engine.begin() as connection:
            await connection.execute(
                text(
                    """
                    INSERT INTO users (normalized_email, name, password_hash, role, status)
                    VALUES (:email, 'name', :password_hash, 'SYSTEM', 'ACTIVE')
                    """
                ),
                {"email": "invalid-role@example.net", "password_hash": "hash"},
            )

    with pytest.raises(IntegrityError):
        async with auth_engine.begin() as connection:
            await connection.execute(
                text(
                    """
                    INSERT INTO users (normalized_email, name, password_hash, role, status)
                    VALUES (:email, 'name', 'hash', 'USER', 'PENDING')
                    """
                ),
                {"email": "invalid-status@example.net"},
            )

    with pytest.raises(IntegrityError):
        async with auth_engine.begin() as connection:
            await connection.execute(
                text(
                    """
                    INSERT INTO users (normalized_email, name, password_hash, role, status)
                    VALUES (:email, 'name', 'hash', 'USER', 'ACTIVE')
                    """
                ),
                {"email": f"{'a' * 243}@example.net"},
            )


@pytest.mark.integration
async def test_security_event_repository_inserts_only_contract_fields(
    auth_engine: AsyncEngine,
) -> None:
    sessions = create_session_factory(auth_engine)
    async with sessions.begin() as session:
        user_repository = SqlAlchemyUserRepository(session)
        user = await user_repository.add(_new_user())
        event_repository = SqlAlchemyUserSecurityEventRepository(session)
        events = [
            NewUserSecurityEvent(
                event_type=SecurityEventType.INITIAL_SUPER_ADMIN_BOOTSTRAPPED,
                subject_user_id=user.id,
                initiator_type=SecurityEventInitiatorType.BOOTSTRAP_CLI,
                new_role=UserRole.SUPER_ADMIN,
                new_status=UserStatus.ACTIVE,
            ),
            NewUserSecurityEvent(
                event_type=SecurityEventType.USER_ROLE_CHANGED,
                subject_user_id=user.id,
                initiator_type=SecurityEventInitiatorType.AUTHENTICATED_USER,
                initiator_user_id=user.id,
                initiator_role=UserRole.SUPER_ADMIN,
                previous_role=UserRole.USER,
                new_role=UserRole.ADMIN,
            ),
            NewUserSecurityEvent(
                event_type=SecurityEventType.USER_DISABLED,
                subject_user_id=user.id,
                initiator_type=SecurityEventInitiatorType.AUTHENTICATED_USER,
                initiator_user_id=user.id,
                initiator_role=UserRole.SUPER_ADMIN,
                previous_status=UserStatus.ACTIVE,
                new_status=UserStatus.DISABLED,
            ),
            NewUserSecurityEvent(
                event_type=SecurityEventType.USER_ENABLED,
                subject_user_id=user.id,
                initiator_type=SecurityEventInitiatorType.AUTHENTICATED_USER,
                initiator_user_id=user.id,
                initiator_role=UserRole.SUPER_ADMIN,
                previous_status=UserStatus.DISABLED,
                new_status=UserStatus.ACTIVE,
            ),
            NewUserSecurityEvent(
                event_type=SecurityEventType.PASSWORD_CHANGED,
                subject_user_id=user.id,
                initiator_type=SecurityEventInitiatorType.AUTHENTICATED_USER,
                initiator_user_id=user.id,
                initiator_role=UserRole.SUPER_ADMIN,
            ),
            NewUserSecurityEvent(
                event_type=SecurityEventType.PASSWORD_RESET,
                subject_user_id=user.id,
                initiator_type=SecurityEventInitiatorType.RESET_TOKEN,
            ),
        ]
        inserted_events = [await event_repository.add(event) for event in events]

    async with auth_engine.connect() as connection:
        user_column_names = await connection.run_sync(
            lambda sync_connection: {
                column["name"] for column in inspect(sync_connection).get_columns("users")
            }
        )
        column_names = await connection.run_sync(
            lambda sync_connection: {
                column["name"]
                for column in inspect(sync_connection).get_columns("user_security_events")
            }
        )
        table_names = await connection.run_sync(
            lambda sync_connection: set(inspect(sync_connection).get_table_names())
        )

    assert len(inserted_events) == 6
    assert all(event.id.version == 7 for event in inserted_events)
    assert all(event.subject_user_id == user.id for event in inserted_events)
    assert "normalized_email" in user_column_names
    assert "email" not in user_column_names
    assert "role" in user_column_names
    assert "user_roles" not in table_names
    assert not {
        "email",
        "name",
        "client_ip",
        "password",
        "password_hash",
        "credential",
        "token",
        "digest",
        "access_jwt",
        "jti",
    }.intersection(column_names)
    assert not hasattr(SqlAlchemyUserSecurityEventRepository, "update")
    assert not hasattr(SqlAlchemyUserSecurityEventRepository, "delete")


@pytest.mark.integration
async def test_database_rejects_security_event_with_crossed_shape(
    auth_engine: AsyncEngine,
) -> None:
    sessions = create_session_factory(auth_engine)
    async with sessions.begin() as session:
        user = await SqlAlchemyUserRepository(session).add(_new_user())

    with pytest.raises(IntegrityError):
        async with auth_engine.begin() as connection:
            await connection.execute(
                text(
                    """
                    INSERT INTO user_security_events (
                        event_type,
                        subject_user_id,
                        initiator_type,
                        initiator_user_id,
                        initiator_role,
                        previous_role,
                        new_role
                    ) VALUES (
                        'PASSWORD_RESET',
                        :subject_user_id,
                        'AUTHENTICATED_USER',
                        :subject_user_id,
                        'ADMIN',
                        'USER',
                        'ADMIN'
                    )
                    """
                ),
                {"subject_user_id": user.id},
            )

    with pytest.raises(IntegrityError):
        async with auth_engine.begin() as connection:
            await connection.execute(
                text(
                    """
                    INSERT INTO user_security_events (
                        event_type,
                        subject_user_id,
                        initiator_type,
                        initiator_user_id
                    ) VALUES (
                        'PASSWORD_CHANGED',
                        :subject_user_id,
                        'AUTHENTICATED_USER',
                        :subject_user_id
                    )
                    """
                ),
                {"subject_user_id": user.id},
            )

    with pytest.raises(IntegrityError):
        async with auth_engine.begin() as connection:
            await connection.execute(
                text(
                    """
                    INSERT INTO user_security_events (
                        event_type,
                        subject_user_id,
                        initiator_type,
                        initiator_user_id,
                        initiator_role,
                        new_role
                    ) VALUES (
                        'USER_ROLE_CHANGED',
                        :subject_user_id,
                        'AUTHENTICATED_USER',
                        :subject_user_id,
                        'ADMIN',
                        'ADMIN'
                    )
                    """
                ),
                {"subject_user_id": user.id},
            )

    with pytest.raises(IntegrityError):
        async with auth_engine.begin() as connection:
            await connection.execute(
                text(
                    """
                    INSERT INTO user_security_events (
                        event_type,
                        subject_user_id,
                        initiator_type,
                        initiator_user_id,
                        initiator_role
                    ) VALUES (
                        'PASSWORD_RESET',
                        :subject_user_id,
                        'RESET_TOKEN',
                        :subject_user_id,
                        'ADMIN'
                    )
                    """
                ),
                {"subject_user_id": user.id},
            )
