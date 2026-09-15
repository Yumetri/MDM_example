from typing import cast
from uuid import UUID

import pytest
from asyncpg import (
    CannotConnectNowError,
    ConnectionDoesNotExistError,
    InvalidPasswordError,
    TooManyConnectionsError,
)
from sqlalchemy.exc import DBAPIError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mdm.application.audit_logs import AuditLogQuery, AuditLogRepositoryUnavailable
from mdm.infrastructure.repositories.audit_logs import SqlAlchemyAuditLogRepository

pytestmark = pytest.mark.unit


class DriverError(Exception):
    def __init__(self, sqlstate):
        self.sqlstate = sqlstate
        super().__init__("internal database diagnostic")


class FailingSession:
    def __init__(self, error):
        self.error = error

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def execute(self, statement):
        raise self.error


def repository(error):
    return SqlAlchemyAuditLogRepository(
        cast(async_sessionmaker[AsyncSession], lambda: FailingSession(error))
    )


@pytest.mark.parametrize("sqlstate", ["57014", "55P03", "40001", "40P01", "08006"])
async def test_transient_sqlstate_is_sanitized_as_repository_unavailable(sqlstate):
    error = DBAPIError("SELECT internal", {}, DriverError(sqlstate))
    with pytest.raises(AuditLogRepositoryUnavailable) as captured:
        await repository(error).list_logs(
            AuditLogQuery(change_set_id=UUID(int=1)), after=None, limit=50
        )
    assert str(captured.value) == ""
    assert captured.value.__suppress_context__


@pytest.mark.parametrize(
    "error",
    [ProgrammingError("broken SQL", {}, DriverError("42601")), InvalidPasswordError("bad config")],
)
async def test_nontransient_error_remains_an_error_instead_of_a_temporary_outage(error):
    with pytest.raises(type(error)) as captured:
        await repository(error).list_logs(
            AuditLogQuery(change_set_id=UUID(int=1)), after=None, limit=50
        )
    assert captured.value is error


async def test_invalidated_connection_is_repository_unavailable():
    error = DBAPIError("SELECT internal", {}, DriverError(None), connection_invalidated=True)
    with pytest.raises(AuditLogRepositoryUnavailable):
        await repository(error).list_logs(
            AuditLogQuery(change_set_id=UUID(int=1)), after=None, limit=50
        )


@pytest.mark.parametrize(
    "error",
    [
        ConnectionRefusedError("connection refused"),
        TimeoutError("connect timed out"),
        OSError("name resolution failed"),
        CannotConnectNowError("database is starting up"),
        TooManyConnectionsError("too many clients"),
        ConnectionDoesNotExistError("connection was closed"),
    ],
)
async def test_connection_establishment_failures_are_repository_unavailable(error):
    from sqlalchemy.ext.asyncio import create_async_engine

    async def fail_connect():
        raise error

    engine = create_async_engine("postgresql+asyncpg://unused", async_creator=fail_connect)
    try:
        repo = SqlAlchemyAuditLogRepository(async_sessionmaker(engine))
        with pytest.raises(AuditLogRepositoryUnavailable):
            await repo.list_logs(AuditLogQuery(change_set_id=UUID(int=1)), after=None, limit=50)
    finally:
        await engine.dispose()
