"""SQLAlchemy adapters for users and permanent security events."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from mdm.application.users import (
    DuplicateUserEmail,
    NewUser,
    NewUserSecurityEvent,
    UserPersistenceError,
)
from mdm.domain.auth import (
    DisplayName,
    EmailAddress,
    SecurityEventInitiatorType,
    SecurityEventType,
    User,
    UserRole,
    UserSecurityEvent,
    UserStatus,
)
from mdm.infrastructure.models import UserRecord, UserSecurityEventRecord


class SqlAlchemyUserRepository:
    """Persist users inside a caller-owned session and transaction."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, new_user: NewUser) -> User:
        record = UserRecord(
            normalized_email=new_user.email.value,
            name=new_user.name.value,
            password_hash=new_user.password_hash,
            role=new_user.role.value,
            status=new_user.status.value,
        )
        self._session.add(record)
        try:
            await self._session.flush()
        except IntegrityError as error:
            if _constraint_name(error) == "uq_users_normalized_email":
                raise DuplicateUserEmail("normalized email already exists") from None
            raise UserPersistenceError("user persistence failed") from None
        except SQLAlchemyError:
            raise UserPersistenceError("user persistence failed") from None
        return _user_from_record(record)

    async def get_by_id(self, user_id: UUID) -> User | None:
        try:
            record = await self._session.get(UserRecord, user_id)
        except SQLAlchemyError:
            raise UserPersistenceError("user persistence failed") from None
        return None if record is None else _user_from_record(record)

    async def get_by_email(self, email: EmailAddress) -> User | None:
        try:
            record = await self._session.scalar(
                select(UserRecord).where(UserRecord.normalized_email == email.value)
            )
        except SQLAlchemyError:
            raise UserPersistenceError("user persistence failed") from None
        return None if record is None else _user_from_record(record)


class SqlAlchemyUserSecurityEventRepository:
    """Expose only insert operations for permanent security events."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, event: NewUserSecurityEvent) -> UserSecurityEvent:
        record = UserSecurityEventRecord(
            event_type=event.event_type.value,
            subject_user_id=event.subject_user_id,
            initiator_type=event.initiator_type.value,
            initiator_user_id=event.initiator_user_id,
            initiator_role=_enum_value(event.initiator_role),
            previous_role=_enum_value(event.previous_role),
            new_role=_enum_value(event.new_role),
            previous_status=_enum_value(event.previous_status),
            new_status=_enum_value(event.new_status),
        )
        self._session.add(record)
        try:
            await self._session.flush()
        except SQLAlchemyError:
            raise UserPersistenceError("user security event persistence failed") from None
        return _event_from_record(record)


def _constraint_name(error: IntegrityError) -> str | None:
    candidate: object | None = error.orig
    while candidate is not None:
        name = getattr(candidate, "constraint_name", None)
        if isinstance(name, str):
            return name
        candidate = getattr(candidate, "__cause__", None)
    return None


def _enum_value(value: UserRole | UserStatus | None) -> str | None:
    return None if value is None else value.value


def _user_from_record(record: UserRecord) -> User:
    return User(
        id=record.id,
        email=EmailAddress(record.normalized_email),
        name=DisplayName(record.name),
        password_hash=record.password_hash,
        role=UserRole(record.role),
        status=UserStatus(record.status),
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def _event_from_record(record: UserSecurityEventRecord) -> UserSecurityEvent:
    return UserSecurityEvent(
        id=record.id,
        event_type=SecurityEventType(record.event_type),
        subject_user_id=record.subject_user_id,
        occurred_at=record.occurred_at,
        initiator_type=SecurityEventInitiatorType(record.initiator_type),
        initiator_user_id=record.initiator_user_id,
        initiator_role=None if record.initiator_role is None else UserRole(record.initiator_role),
        previous_role=None if record.previous_role is None else UserRole(record.previous_role),
        new_role=None if record.new_role is None else UserRole(record.new_role),
        previous_status=(
            None if record.previous_status is None else UserStatus(record.previous_status)
        ),
        new_status=None if record.new_status is None else UserStatus(record.new_status),
    )
