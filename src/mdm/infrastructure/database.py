"""Asynchronous SQLAlchemy database adapters."""

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from mdm.application.health import ReadinessUnavailable


def create_engine(database_url: str) -> AsyncEngine:
    """Create the application's async SQLAlchemy engine."""
    return create_async_engine(database_url, pool_pre_ping=True)


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Create request- or transaction-scoped async sessions."""
    return async_sessionmaker(engine, expire_on_commit=False)


class SqlAlchemyDatabaseProbe:
    """Verify database availability without exposing SQLAlchemy upstream."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def ping(self) -> None:
        try:
            async with self._engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
        except SQLAlchemyError:
            raise ReadinessUnavailable from None
