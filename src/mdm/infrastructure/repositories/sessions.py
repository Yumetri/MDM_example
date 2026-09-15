"""Short-lived reads and ordered PostgreSQL credential transactions."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mdm.application.sessions import (
    RefreshDigestConflict,
    RefreshFamily,
    RefreshTarget,
    SessionTransaction,
    SessionUnavailable,
    StoredRefreshToken,
)
from mdm.domain.auth import EmailAddress, User
from mdm.domain.credentials import RefreshToken
from mdm.infrastructure.models import RefreshFamilyRecord, RefreshTokenRecord, UserRecord
from mdm.infrastructure.repositories.users import (
    SqlAlchemyUserRepository,
    _constraint_name,
    _user_from_record,
)


class SqlAlchemySessionTransaction:
    def __init__(self, session: AsyncSession, user: User | None) -> None:
        self._session = session
        self.user = user

    async def lock_families(self, family_id: UUID | None = None) -> tuple[RefreshFamily, ...]:
        if self.user is None:
            return ()
        records = await self._session.scalars(
            select(RefreshFamilyRecord)
            .where(
                RefreshFamilyRecord.user_id == self.user.id,
                RefreshFamilyRecord.status == "ACTIVE"
                if family_id is None
                else RefreshFamilyRecord.id == family_id,
            )
            .order_by(RefreshFamilyRecord.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return tuple(
            RefreshFamily(row.id, row.user_id, row.status, row.expires_at) for row in records
        )

    async def lock_tokens(self, family_id: UUID, token_id: UUID) -> tuple[StoredRefreshToken, ...]:
        records = await self._session.scalars(
            select(RefreshTokenRecord)
            .where(RefreshTokenRecord.family_id == family_id, RefreshTokenRecord.id == token_id)
            .order_by(RefreshTokenRecord.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )

        return tuple(
            StoredRefreshToken(row.id, row.family_id, row.digest, row.used_at) for row in records
        )

    async def mark_used(
        self, token_id: UUID, replacement_id: UUID, family_id: UUID, now: datetime
    ) -> None:
        await self._session.execute(
            update(RefreshTokenRecord)
            .where(RefreshTokenRecord.id == token_id)
            .values(used_at=now, replaced_by_id=replacement_id)
        )
        await self._session.execute(
            update(RefreshFamilyRecord)
            .where(RefreshFamilyRecord.id == family_id)
            .values(updated_at=now)
        )

    async def now(self) -> datetime:
        value = await self._session.scalar(select(func.clock_timestamp()))
        assert isinstance(value, datetime)
        return value

    async def revoke(self, family_id: UUID, now: datetime) -> None:
        await self._session.execute(
            update(RefreshFamilyRecord)
            .where(RefreshFamilyRecord.id == family_id, RefreshFamilyRecord.status == "ACTIVE")
            .values(status="REVOKED", revoked_at=now, updated_at=now)
        )

    async def create_family(self, now: datetime, expires_at: datetime) -> UUID:
        assert self.user is not None
        row = RefreshFamilyRecord(
            user_id=self.user.id,
            status="ACTIVE",
            expires_at=expires_at,
            created_at=now,
            updated_at=now,
        )
        self._session.add(row)
        await self._session.flush()
        return row.id

    async def add_token(self, family_id: UUID, token: RefreshToken, now: datetime) -> UUID:
        try:
            async with self._session.begin_nested():
                row = RefreshTokenRecord(family_id=family_id, digest=token.digest(), issued_at=now)
                self._session.add(row)
                await self._session.flush()
                token_id = row.id
        except IntegrityError as exc:
            if _constraint_name(exc) == "uq_refresh_tokens_digest":
                raise RefreshDigestConflict from None
            raise
        return token_id


class SqlAlchemySessionRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get_by_email(self, email: EmailAddress) -> User | None:
        async with self._session_factory() as session:
            return await SqlAlchemyUserRepository(session).get_by_email(email)

    async def get_by_id(self, user_id: UUID) -> User | None:
        async with self._session_factory() as session:
            return await SqlAlchemyUserRepository(session).get_by_id(user_id)

    async def find_refresh(self, digest: bytes) -> RefreshTarget | None:
        try:
            async with self._session_factory() as session:
                row = (
                    await session.execute(
                        select(
                            RefreshFamilyRecord.user_id,
                            RefreshTokenRecord.family_id,
                            RefreshTokenRecord.id,
                        )
                        .join(
                            RefreshFamilyRecord,
                            RefreshFamilyRecord.id == RefreshTokenRecord.family_id,
                        )
                        .where(RefreshTokenRecord.digest == digest)
                    )
                ).one_or_none()
                return None if row is None else RefreshTarget(row[0], row[1], row[2])
        except SQLAlchemyError:
            raise SessionUnavailable("session persistence failed") from None

    @asynccontextmanager
    async def transaction(self, user_id: UUID) -> AsyncIterator[SessionTransaction]:
        try:
            async with self._session_factory.begin() as session:
                row = await session.scalar(
                    select(UserRecord)
                    .where(UserRecord.id == user_id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
                user = None if row is None else _user_from_record(row)
                yield SqlAlchemySessionTransaction(session, user)
        except SQLAlchemyError:
            raise SessionUnavailable("session persistence failed") from None
