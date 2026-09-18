"""Read-only user projections and administrator query boundaries."""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from mdm.application.auth import HumanPrincipal
from mdm.application.authorization import AuthorizationAction, AuthorizationPolicy
from mdm.domain.auth import EmailAddress, UserRole, UserStatus


class UserNotFound(RuntimeError):
    """The user does not exist within the caller's visible scope."""


class UserQueryUnavailable(RuntimeError):
    """User reads failed without exposing database diagnostics."""


class UserQueryValidationError(ValueError):
    def __init__(self, field: str, message: str):
        self.field = field
        self.message = message
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class UserSummary:
    id: UUID
    email: str
    name: str
    role: UserRole
    status: UserStatus
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class UserFilters:
    email: EmailAddress | None = None
    role: UserRole | None = None
    status: UserStatus | None = None


@dataclass(frozen=True, slots=True)
class UserCursor:
    created_at: datetime
    id: UUID

    def __post_init__(self) -> None:
        if self.created_at.utcoffset() is None or not isinstance(self.id, UUID):
            raise ValueError("user cursor requires an aware timestamp and UUID")


@dataclass(frozen=True, slots=True)
class UserPage:
    items: tuple[UserSummary, ...]
    has_more: bool


class AdminUserRepository(Protocol):
    async def list_users(
        self,
        *,
        visible_role: UserRole | None,
        filters: UserFilters,
        after: UserCursor | None,
        limit: int,
    ) -> UserPage: ...

    async def get_user(
        self,
        user_id: UUID,
        *,
        visible_role: UserRole | None,
    ) -> UserSummary | None: ...


class AdminUserQueries:
    def __init__(self, repository: AdminUserRepository, authorization: AuthorizationPolicy):
        self._repository = repository
        self._authorization = authorization

    def visible_role(self, principal: HumanPrincipal) -> UserRole | None:
        """Resolve the authorized scope shared by pagination and database reads."""
        self._authorization.authorize(principal, AuthorizationAction.READ_USERS)
        return UserRole.USER if principal.role == UserRole.ADMIN else None

    async def list_users(
        self,
        principal: HumanPrincipal,
        filters: UserFilters,
        *,
        after: UserCursor | None,
        limit: int,
    ) -> UserPage:
        visible_role = self.visible_role(principal)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise UserQueryValidationError("query.limit", "페이지 크기는 1~100이어야 합니다.")
        return await self._repository.list_users(
            visible_role=visible_role,
            filters=filters,
            after=after,
            limit=limit,
        )

    async def get_user(self, principal: HumanPrincipal, user_id: UUID) -> UserSummary:
        visible_role = self.visible_role(principal)
        user = await self._repository.get_user(user_id, visible_role=visible_role)
        if user is None:
            raise UserNotFound
        return user
