import traceback
from typing import cast
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest
from asyncpg import CannotConnectNowError, TooManyConnectionsError
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mdm.application.admin_users import UserFilters, UserQueryUnavailable
from mdm.domain.auth import UserRole
from mdm.infrastructure.repositories.admin_users import SqlAlchemyAdminUserRepository

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("detail", [False, True])
@pytest.mark.parametrize(
    "failure",
    [
        OperationalError("SELECT private_table", {}, RuntimeError("password=private-secret")),
        ConnectionRefusedError("private-secret"),
        TimeoutError("private-secret"),
        CannotConnectNowError("private-secret"),
        TooManyConnectionsError("private-secret"),
    ],
)
async def test_database_failure_is_sanitized_and_session_is_closed(detail, failure):
    session = MagicMock()
    session.execute = AsyncMock(side_effect=failure)
    factory = MagicMock()
    factory.return_value.__aenter__.return_value = session
    repository = SqlAlchemyAdminUserRepository(cast(async_sessionmaker[AsyncSession], factory))
    with pytest.raises(UserQueryUnavailable) as raised:
        if detail:
            await repository.get_user(UUID(int=1), visible_role=UserRole.USER)
        else:
            await repository.list_users(
                visible_role=UserRole.USER, filters=UserFilters(), after=None, limit=50
            )
    assert "private-secret" not in "".join(traceback.format_exception(raised.value))
    assert "private_table" not in "".join(traceback.format_exception(raised.value))
    factory.return_value.__aexit__.assert_awaited_once()
    session.begin.return_value.__aexit__.assert_awaited_once()


@pytest.mark.parametrize("detail", [False, True])
@pytest.mark.parametrize("failure", [CannotConnectNowError, TooManyConnectionsError])
async def test_raw_driver_connection_initialization_failure_is_sanitized(detail, failure):
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import AsyncAdaptedQueuePool

    from mdm.infrastructure.database import create_session_factory

    async def unavailable_connection():
        raise failure("private-secret")

    engine = create_async_engine("postgresql+asyncpg://", async_creator=unavailable_connection)
    try:
        repository = SqlAlchemyAdminUserRepository(create_session_factory(engine))
        with pytest.raises(UserQueryUnavailable) as raised:
            if detail:
                await repository.get_user(UUID(int=1), visible_role=UserRole.USER)
            else:
                await repository.list_users(
                    visible_role=UserRole.USER, filters=UserFilters(), after=None, limit=50
                )
        assert "private-secret" not in "".join(traceback.format_exception(raised.value))
        assert isinstance(engine.pool, AsyncAdaptedQueuePool)
        assert engine.pool.checkedout() == 0
    finally:
        await engine.dispose()
