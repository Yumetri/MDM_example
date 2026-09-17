"""Project only public user fields, with visibility applied in every SQL read."""

from typing import Any
from uuid import UUID

from asyncpg import PostgresError
from sqlalchemy import Select, literal, select, tuple_
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mdm.application.admin_users import (
    UserCursor,
    UserFilters,
    UserPage,
    UserQueryUnavailable,
    UserSummary,
)
from mdm.domain.auth import UserRole, UserStatus
from mdm.infrastructure.models import UserRecord


def _visible_users(visible_role: UserRole | None) -> Select[Any]:
    statement = select(
        UserRecord.id,
        UserRecord.normalized_email.label("email"),
        UserRecord.name,
        UserRecord.role,
        UserRecord.status,
        UserRecord.created_at,
        UserRecord.updated_at,
    )
    if visible_role is not None:
        statement = statement.where(UserRecord.role == visible_role.value)
    return statement


def _summary(row: Any) -> UserSummary:
    return UserSummary(
        id=row.id,
        email=row.email,
        name=row.name,
        role=UserRole(row.role),
        status=UserStatus(row.status),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


class SqlAlchemyAdminUserRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]):
        self._session_factory = session_factory

    async def list_users(
        self,
        *,
        visible_role: UserRole | None,
        filters: UserFilters,
        after: UserCursor | None,
        limit: int,
    ) -> UserPage:
        statement = _visible_users(visible_role)
        if filters.email is not None:
            statement = statement.where(UserRecord.normalized_email == filters.email.value)
        if filters.role is not None:
            statement = statement.where(UserRecord.role == filters.role.value)
        if filters.status is not None:
            statement = statement.where(UserRecord.status == filters.status.value)
        if after is not None:
            statement = statement.where(
                tuple_(UserRecord.created_at, UserRecord.id)
                < tuple_(literal(after.created_at), literal(after.id))
            )
        statement = statement.order_by(UserRecord.created_at.desc(), UserRecord.id.desc()).limit(
            limit + 1
        )
        try:
            async with self._session_factory() as session, session.begin():
                rows = (await session.execute(statement)).all()
        except (SQLAlchemyError, PostgresError, OSError):
            raise UserQueryUnavailable from None
        return UserPage(tuple(_summary(row) for row in rows[:limit]), len(rows) > limit)

    async def get_user(
        self,
        user_id: UUID,
        *,
        visible_role: UserRole | None,
    ) -> UserSummary | None:
        statement = _visible_users(visible_role).where(UserRecord.id == user_id)
        try:
            async with self._session_factory() as session, session.begin():
                row = (await session.execute(statement)).one_or_none()
        except (SQLAlchemyError, PostgresError, OSError):
            raise UserQueryUnavailable from None
        return None if row is None else _summary(row)
