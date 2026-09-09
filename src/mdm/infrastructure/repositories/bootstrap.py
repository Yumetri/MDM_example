"""Transaction-owning PostgreSQL adapter for SUPER_ADMIN bootstrap."""

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mdm.application.users import NewUser, NewUserSecurityEvent
from mdm.domain.auth import (
    EmailAddress,
    SecurityEventInitiatorType,
    SecurityEventType,
    User,
    UserRole,
    UserStatus,
)
from mdm.infrastructure.repositories.users import (
    SqlAlchemyUserRepository,
    SqlAlchemyUserSecurityEventRepository,
)


class SqlAlchemyBootstrapSuperAdminRepository:
    """Keep reads short and atomically persist the initial user and audit."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get_by_email(self, email: EmailAddress) -> User | None:
        async with self._session_factory() as session:
            return await SqlAlchemyUserRepository(session).get_by_email(email)

    async def create_with_security_event(self, new_user: NewUser) -> User:
        async with self._session_factory.begin() as session:
            user = await SqlAlchemyUserRepository(session).add(new_user)
            await SqlAlchemyUserSecurityEventRepository(session).add(
                NewUserSecurityEvent(
                    event_type=SecurityEventType.INITIAL_SUPER_ADMIN_BOOTSTRAPPED,
                    subject_user_id=user.id,
                    initiator_type=SecurityEventInitiatorType.BOOTSTRAP_CLI,
                    new_role=UserRole.SUPER_ADMIN,
                    new_status=UserStatus.ACTIVE,
                )
            )
            return user
