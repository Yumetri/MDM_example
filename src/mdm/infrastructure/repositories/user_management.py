"""Target-user transactions compatible with security-audit foreign-key locks."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from uuid import UUID

from asyncpg import PostgresError
from sqlalchemy import select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mdm.application.user_management import UserManagementTransaction, UserManagementUnavailable
from mdm.application.users import NewUserSecurityEvent
from mdm.domain.auth import UserRole, UserStatus
from mdm.infrastructure.models import UserRecord, UserSecurityEventRecord
from mdm.infrastructure.repositories.password_lifecycle import SqlAlchemyPasswordTransaction
from mdm.infrastructure.repositories.users import _user_from_record


class SqlAlchemyUserManagementTransaction(SqlAlchemyPasswordTransaction):
    async def set_role(self, role: UserRole, now: datetime) -> None:
        assert self.user is not None
        await self._session.execute(
            update(UserRecord)
            .where(UserRecord.id == self.user.id)
            .values(role=role.value, updated_at=now)
        )

    async def set_status(self, status: UserStatus, now: datetime) -> None:
        assert self.user is not None
        await self._session.execute(
            update(UserRecord)
            .where(UserRecord.id == self.user.id)
            .values(status=status.value, updated_at=now)
        )

    async def audit(self, event: NewUserSecurityEvent, now: datetime) -> None:
        self._session.add(
            UserSecurityEventRecord(
                event_type=event.event_type.value,
                subject_user_id=event.subject_user_id,
                occurred_at=now,
                initiator_type=event.initiator_type.value,
                initiator_user_id=event.initiator_user_id,
                initiator_role=event.initiator_role,
                previous_role=event.previous_role,
                new_role=event.new_role,
                previous_status=event.previous_status,
                new_status=event.new_status,
            )
        )
        await self._session.flush()


class SqlAlchemyUserManagementRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]):
        self._session_factory = session_factory

    @asynccontextmanager
    async def transaction(self, user_id: UUID) -> AsyncIterator[UserManagementTransaction]:
        try:
            async with self._session_factory.begin() as session:
                row = await session.scalar(
                    select(UserRecord)
                    .where(UserRecord.id == user_id)
                    # PostgreSQL FOR NO KEY UPDATE: serialize mutations while allowing
                    # audit FK KEY SHARE locks during mutual administrator changes.
                    .with_for_update(key_share=True)
                    .execution_options(populate_existing=True)
                )
                yield SqlAlchemyUserManagementTransaction(
                    session, None if row is None else _user_from_record(row)
                )
        except (SQLAlchemyError, PostgresError, OSError):
            raise UserManagementUnavailable("user management persistence failed") from None
