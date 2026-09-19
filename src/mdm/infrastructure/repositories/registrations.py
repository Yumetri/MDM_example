"""Digest-only registration persistence and per-email transaction serialization."""

import hashlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from uuid import UUID

from asyncpg import PostgresError
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mdm.application.registrations import (
    RegistrationChallenge,
    RegistrationDigestConflict,
    RegistrationTarget,
    RegistrationTransaction,
    RegistrationUnavailable,
    StoredRegistrationToken,
)
from mdm.application.users import NewUser, UserPersistenceError
from mdm.domain.auth import EmailAddress, User
from mdm.domain.credentials import RefreshToken, RegistrationToken
from mdm.infrastructure.models import (
    RegistrationChallengeRecord,
    RegistrationTokenRecord,
    UserRecord,
)
from mdm.infrastructure.repositories.sessions import SqlAlchemySessionTransaction
from mdm.infrastructure.repositories.users import SqlAlchemyUserRepository, _constraint_name


def _challenge(row: RegistrationChallengeRecord | None) -> RegistrationChallenge | None:
    if row is None:
        return None
    return RegistrationChallenge(
        row.id, EmailAddress(row.normalized_email), row.status, row.expires_at
    )


class SqlAlchemyRegistrationTransaction:
    def __init__(self, session: AsyncSession, email: EmailAddress) -> None:
        self._session = session
        self._email = email

    async def user_exists(self) -> bool:
        return (
            await self._session.scalar(
                select(UserRecord.id).where(UserRecord.normalized_email == self._email.value)
            )
            is not None
        )

    async def lock_active_challenge(self) -> RegistrationChallenge | None:
        row = await self._session.scalar(
            select(RegistrationChallengeRecord)
            .where(
                RegistrationChallengeRecord.normalized_email == self._email.value,
                RegistrationChallengeRecord.status == "ACTIVE",
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return _challenge(row)

    async def lock_challenge(self, challenge_id: UUID) -> RegistrationChallenge | None:
        row = await self._session.scalar(
            select(RegistrationChallengeRecord)
            .where(
                RegistrationChallengeRecord.id == challenge_id,
                RegistrationChallengeRecord.normalized_email == self._email.value,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return _challenge(row)

    async def lock_token(
        self, challenge_id: UUID, token_id: UUID
    ) -> StoredRegistrationToken | None:
        row = await self._session.scalar(
            select(RegistrationTokenRecord)
            .where(
                RegistrationTokenRecord.id == token_id,
                RegistrationTokenRecord.challenge_id == challenge_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if row is None:
            return None
        return StoredRegistrationToken(row.id, row.challenge_id, row.digest, row.used_at)

    async def now(self) -> datetime:
        now = await self._session.scalar(select(func.clock_timestamp()))
        assert isinstance(now, datetime)
        return now

    async def expire(self, challenge_id: UUID) -> None:
        await self._session.execute(
            update(RegistrationChallengeRecord)
            .where(RegistrationChallengeRecord.id == challenge_id)
            .values(status="EXPIRED")
        )

    async def create_challenge(self, now: datetime, expires_at: datetime) -> RegistrationChallenge:
        row = RegistrationChallengeRecord(
            normalized_email=self._email.value,
            status="ACTIVE",
            created_at=now,
            expires_at=expires_at,
        )
        self._session.add(row)
        await self._session.flush()
        return RegistrationChallenge(row.id, self._email, row.status, row.expires_at)

    async def add_registration_token(
        self, challenge_id: UUID, token: RegistrationToken, now: datetime
    ) -> None:
        try:
            async with self._session.begin_nested():
                self._session.add(
                    RegistrationTokenRecord(
                        challenge_id=challenge_id, digest=token.digest(), issued_at=now
                    )
                )
                await self._session.flush()
        except IntegrityError as exc:
            if _constraint_name(exc) == "uq_registration_tokens_digest":
                raise RegistrationDigestConflict from None
            raise

    async def create_user(self, values: NewUser) -> User:
        return await SqlAlchemyUserRepository(self._session).add(values)

    async def create_family(self, user: User, now: datetime, expires_at: datetime) -> UUID:
        return await SqlAlchemySessionTransaction(self._session, user).create_family(
            now, expires_at
        )

    async def add_refresh_token(self, family_id: UUID, token: RefreshToken, now: datetime) -> None:
        await SqlAlchemySessionTransaction(self._session, None).add_token(family_id, token, now)

    async def complete(self, challenge_id: UUID, token_id: UUID, now: datetime) -> None:
        await self._session.execute(
            update(RegistrationTokenRecord)
            .where(
                RegistrationTokenRecord.id == token_id,
                RegistrationTokenRecord.challenge_id == challenge_id,
            )
            .values(used_at=now)
        )
        await self._session.execute(
            update(RegistrationChallengeRecord)
            .where(RegistrationChallengeRecord.id == challenge_id)
            .values(status="COMPLETED", completed_at=now)
        )


class SqlAlchemyRegistrationRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def find_token(self, digest: bytes) -> RegistrationTarget | None:
        try:
            async with self._session_factory() as session:
                row = (
                    await session.execute(
                        select(
                            RegistrationTokenRecord.challenge_id,
                            RegistrationTokenRecord.id,
                            RegistrationChallengeRecord.normalized_email,
                        )
                        .join(
                            RegistrationChallengeRecord,
                            RegistrationChallengeRecord.id == RegistrationTokenRecord.challenge_id,
                        )
                        .where(RegistrationTokenRecord.digest == digest)
                    )
                ).one_or_none()
                return (
                    None
                    if row is None
                    else RegistrationTarget(row[0], row[1], EmailAddress(row[2]))
                )
        except (SQLAlchemyError, PostgresError, OSError):
            raise RegistrationUnavailable("registration persistence failed") from None

    @asynccontextmanager
    async def transaction(self, email: EmailAddress) -> AsyncIterator[RegistrationTransaction]:
        # Stable across processes; unlike hash(), this key does not change at interpreter startup.
        # A rare key collision only serializes unrelated emails; identity is always checked in SQL.
        digest = hashlib.sha256(b"mdm:registration:" + email.value.encode("utf-8")).digest()
        key = int.from_bytes(digest[:8], "big", signed=True)
        try:
            async with self._session_factory.begin() as session:
                await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})
                yield SqlAlchemyRegistrationTransaction(session, email)
        except (SQLAlchemyError, PostgresError, OSError, UserPersistenceError):
            raise RegistrationUnavailable("registration persistence failed") from None
