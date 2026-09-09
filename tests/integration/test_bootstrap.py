import asyncio
import hashlib
from collections.abc import AsyncGenerator

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine

from mdm.application.bootstrap import BootstrapOutcome, BootstrapSuperAdmin
from mdm.application.users import UserPersistenceError
from mdm.domain.auth import PlainPassword
from mdm.infrastructure.database import create_engine, create_session_factory
from mdm.infrastructure.models import UserRecord, UserSecurityEventRecord
from mdm.infrastructure.repositories.bootstrap import SqlAlchemyBootstrapSuperAdminRepository
from mdm.infrastructure.settings import Settings


class DeterministicPasswordHasher:
    async def hash(self, password: PlainPassword) -> str:
        await asyncio.sleep(0)
        return _test_hash(password)

    async def verify(self, password: PlainPassword, encoded_hash: str) -> bool:
        await asyncio.sleep(0)
        return encoded_hash == _test_hash(password)


def _test_hash(password: PlainPassword) -> str:
    digest = hashlib.sha256(password.reveal().encode()).hexdigest()
    return f"test-hash:{digest}"


@pytest.fixture
async def bootstrap_engine() -> AsyncGenerator[AsyncEngine]:
    engine = create_engine(Settings().reveal_database_url())
    async with engine.begin() as connection:
        await connection.execute(text("TRUNCATE user_security_events, users"))
    yield engine
    await engine.dispose()


@pytest.mark.integration
async def test_concurrent_bootstrap_converges_after_unique_transaction_rollback(
    bootstrap_engine: AsyncEngine,
) -> None:
    sessions = create_session_factory(bootstrap_engine)
    repository = SqlAlchemyBootstrapSuperAdminRepository(sessions)
    use_case = BootstrapSuperAdmin(repository, DeterministicPasswordHasher())

    outcomes = await asyncio.gather(
        use_case.execute(
            email="admin@example.net",
            name="최초 관리자",
            password="correct horse battery staple",
        ),
        use_case.execute(
            email="ADMIN@example.net",
            name="무시되는 동시 이름",
            password="correct horse battery staple",
        ),
    )

    async with sessions() as session:
        user_count = await session.scalar(select(func.count()).select_from(UserRecord))
        event_count = await session.scalar(
            select(func.count()).select_from(UserSecurityEventRecord)
        )
        user = await session.scalar(select(UserRecord))

    assert sorted(outcomes) == sorted([BootstrapOutcome.CREATED, BootstrapOutcome.NO_OP])
    assert user_count == 1
    assert event_count == 1
    assert user is not None
    assert user.role == "SUPER_ADMIN"
    assert user.status == "ACTIVE"


@pytest.mark.integration
async def test_repeated_bootstrap_does_not_mutate_user_or_add_audit(
    bootstrap_engine: AsyncEngine,
) -> None:
    sessions = create_session_factory(bootstrap_engine)
    repository = SqlAlchemyBootstrapSuperAdminRepository(sessions)
    use_case = BootstrapSuperAdmin(repository, DeterministicPasswordHasher())
    arguments = {
        "email": "admin@example.net",
        "name": "최초 관리자",
        "password": "correct horse battery staple",
    }

    assert await use_case.execute(**arguments) is BootstrapOutcome.CREATED
    async with sessions() as session:
        before = await session.scalar(select(UserRecord))
        assert before is not None
        snapshot = (before.name, before.password_hash, before.created_at, before.updated_at)

    assert (
        await use_case.execute(**(arguments | {"name": "달라져도 무시"})) is BootstrapOutcome.NO_OP
    )
    async with sessions() as session:
        after = await session.scalar(select(UserRecord))
        event_count = await session.scalar(
            select(func.count()).select_from(UserSecurityEventRecord)
        )
        assert after is not None
        current = (after.name, after.password_hash, after.created_at, after.updated_at)

    assert current == snapshot
    assert event_count == 1


@pytest.mark.integration
async def test_security_event_failure_rolls_back_the_created_user(
    bootstrap_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def reject_security_event(*args: object, **kwargs: object) -> None:
        raise UserPersistenceError("sanitized event failure")

    monkeypatch.setattr(
        "mdm.infrastructure.repositories.bootstrap.SqlAlchemyUserSecurityEventRepository.add",
        reject_security_event,
    )
    sessions = create_session_factory(bootstrap_engine)
    repository = SqlAlchemyBootstrapSuperAdminRepository(sessions)
    use_case = BootstrapSuperAdmin(repository, DeterministicPasswordHasher())

    with pytest.raises(UserPersistenceError, match="sanitized event failure"):
        await use_case.execute(
            email="admin@example.net",
            name="최초 관리자",
            password="correct horse battery staple",
        )

    async with sessions() as session:
        user_count = await session.scalar(select(func.count()).select_from(UserRecord))
        event_count = await session.scalar(
            select(func.count()).select_from(UserSecurityEventRecord)
        )

    assert user_count == 0
    assert event_count == 0
