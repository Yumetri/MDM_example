"""User-serialized password transactions and digest-only reset persistence."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from uuid import UUID

from asyncpg import PostgresError
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mdm.application.auth import HumanPrincipal
from mdm.application.password_lifecycle import (
    PasswordLifecycleUnavailable,
    PasswordTransaction,
    ResetChallenge,
    ResetDigestConflict,
    ResetTarget,
    StoredResetToken,
)
from mdm.application.sessions import RefreshTarget, StoredRefreshToken
from mdm.domain.auth import EmailAddress, User
from mdm.domain.credentials import PasswordResetToken
from mdm.infrastructure.models import (
    PasswordResetChallengeRecord,
    PasswordResetTokenRecord,
    RefreshFamilyRecord,
    RefreshTokenRecord,
    UserRecord,
    UserSecurityEventRecord,
)
from mdm.infrastructure.repositories.sessions import SqlAlchemySessionTransaction
from mdm.infrastructure.repositories.users import _constraint_name, _user_from_record


def _challenge(row: PasswordResetChallengeRecord) -> ResetChallenge:
    return ResetChallenge(row.id, row.user_id, row.status, row.expires_at)


class SqlAlchemyPasswordTransaction(SqlAlchemySessionTransaction):
    async def lock_refresh_tokens(
        self, family_ids: tuple[UUID, ...]
    ) -> tuple[StoredRefreshToken, ...]:
        records = await self._session.scalars(
            select(RefreshTokenRecord)
            .where(RefreshTokenRecord.family_id.in_(family_ids))
            .order_by(RefreshTokenRecord.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return tuple(StoredRefreshToken(r.id, r.family_id, r.digest, r.used_at) for r in records)

    async def lock_challenges(self, challenge_id: UUID | None = None) -> tuple[ResetChallenge, ...]:
        assert self.user is not None
        records = await self._session.scalars(
            select(PasswordResetChallengeRecord)
            .where(
                PasswordResetChallengeRecord.user_id == self.user.id,
                PasswordResetChallengeRecord.status == "ACTIVE"
                if challenge_id is None
                else PasswordResetChallengeRecord.id == challenge_id,
            )
            .order_by(PasswordResetChallengeRecord.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return tuple(_challenge(r) for r in records)

    async def lock_reset_tokens(
        self, challenge_ids: tuple[UUID, ...]
    ) -> tuple[StoredResetToken, ...]:
        records = await self._session.scalars(
            select(PasswordResetTokenRecord)
            .where(PasswordResetTokenRecord.challenge_id.in_(challenge_ids))
            .order_by(PasswordResetTokenRecord.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return tuple(StoredResetToken(r.id, r.challenge_id, r.digest, r.used_at) for r in records)

    async def expire_challenge(self, challenge_id: UUID) -> None:
        await self._session.execute(
            update(PasswordResetChallengeRecord)
            .where(PasswordResetChallengeRecord.id == challenge_id)
            .values(status="EXPIRED")
        )

    async def create_challenge(self, now: datetime, expires_at: datetime) -> ResetChallenge:
        assert self.user is not None
        row = PasswordResetChallengeRecord(
            user_id=self.user.id, status="ACTIVE", created_at=now, expires_at=expires_at
        )
        self._session.add(row)
        await self._session.flush()
        return _challenge(row)

    async def add_reset_token(
        self, challenge_id: UUID, token: PasswordResetToken, now: datetime
    ) -> None:
        try:
            async with self._session.begin_nested():
                self._session.add(
                    PasswordResetTokenRecord(
                        challenge_id=challenge_id, digest=token.digest(), issued_at=now
                    )
                )
                await self._session.flush()
        except IntegrityError as exc:
            if _constraint_name(exc) == "uq_password_reset_tokens_digest":
                raise ResetDigestConflict from None
            raise

    async def change_hash(self, password_hash: str, now: datetime) -> None:
        assert self.user is not None
        await self._session.execute(
            update(UserRecord)
            .where(UserRecord.id == self.user.id)
            .values(password_hash=password_hash, updated_at=now)
        )

    async def complete_reset(self, challenge_id: UUID, token_id: UUID, now: datetime) -> None:
        await self._session.execute(
            update(PasswordResetTokenRecord)
            .where(
                PasswordResetTokenRecord.id == token_id,
                PasswordResetTokenRecord.challenge_id == challenge_id,
            )
            .values(used_at=now)
        )
        await self._session.execute(
            update(PasswordResetChallengeRecord)
            .where(PasswordResetChallengeRecord.id == challenge_id)
            .values(status="COMPLETED", completed_at=now)
        )

    async def revoke_challenge(self, challenge_id: UUID, now: datetime) -> None:
        await self._session.execute(
            update(PasswordResetChallengeRecord)
            .where(
                PasswordResetChallengeRecord.id == challenge_id,
                PasswordResetChallengeRecord.status == "ACTIVE",
            )
            .values(status="REVOKED", revoked_at=now)
        )

    async def audit_password(self, principal: HumanPrincipal | None, now: datetime) -> None:
        assert self.user is not None
        self._session.add(
            UserSecurityEventRecord(
                event_type="PASSWORD_RESET" if principal is None else "PASSWORD_CHANGED",
                subject_user_id=self.user.id,
                occurred_at=now,
                initiator_type="RESET_TOKEN" if principal is None else "AUTHENTICATED_USER",
                initiator_user_id=None if principal is None else principal.user_id,
                initiator_role=None if principal is None else principal.role.value,
            )
        )
        await self._session.flush()


class SqlAlchemyPasswordRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get_by_email(self, email: EmailAddress) -> User | None:
        try:
            async with self._session_factory() as session:
                row = await session.scalar(
                    select(UserRecord).where(UserRecord.normalized_email == email.value)
                )
                return None if row is None else _user_from_record(row)
        except (SQLAlchemyError, PostgresError, OSError):
            raise PasswordLifecycleUnavailable("password persistence failed") from None

    async def get_by_id(self, user_id: UUID) -> User | None:
        try:
            async with self._session_factory() as session:
                row = await session.get(UserRecord, user_id)
                return None if row is None else _user_from_record(row)
        except (SQLAlchemyError, PostgresError, OSError):
            raise PasswordLifecycleUnavailable("password persistence failed") from None

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
        except (SQLAlchemyError, PostgresError, OSError):
            raise PasswordLifecycleUnavailable("password persistence failed") from None

    async def find_reset(self, digest: bytes) -> ResetTarget | None:
        try:
            async with self._session_factory() as session:
                row = (
                    await session.execute(
                        select(
                            PasswordResetChallengeRecord.user_id,
                            PasswordResetTokenRecord.challenge_id,
                            PasswordResetTokenRecord.id,
                        )
                        .join(
                            PasswordResetChallengeRecord,
                            PasswordResetChallengeRecord.id
                            == PasswordResetTokenRecord.challenge_id,
                        )
                        .where(PasswordResetTokenRecord.digest == digest)
                    )
                ).one_or_none()
                return None if row is None else ResetTarget(row[0], row[1], row[2])
        except (SQLAlchemyError, PostgresError, OSError):
            raise PasswordLifecycleUnavailable("password persistence failed") from None

    @asynccontextmanager
    async def transaction(self, user_id: UUID) -> AsyncIterator[PasswordTransaction]:
        try:
            async with self._session_factory.begin() as session:
                row = await session.scalar(
                    select(UserRecord)
                    .where(UserRecord.id == user_id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
                yield SqlAlchemyPasswordTransaction(
                    session, None if row is None else _user_from_record(row)
                )
        except (SQLAlchemyError, PostgresError, OSError):
            raise PasswordLifecycleUnavailable("password persistence failed") from None
