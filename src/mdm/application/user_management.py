"""Authorized user mutations with atomic credential revocation and typed audits."""

from contextlib import AbstractAsyncContextManager
from datetime import datetime
from typing import Protocol
from uuid import UUID

from mdm.application.admin_users import UserNotFound
from mdm.application.auth import HumanPrincipal
from mdm.application.authorization import (
    AuthorizationAction,
    AuthorizationDenied,
    AuthorizationPolicy,
)
from mdm.application.password_lifecycle import ResetChallenge, StoredResetToken
from mdm.application.sessions import RefreshFamily, StoredRefreshToken
from mdm.application.users import NewUserSecurityEvent
from mdm.domain.auth import (
    SecurityEventInitiatorType,
    SecurityEventType,
    User,
    UserRole,
    UserStatus,
)


class UserManagementUnavailable(RuntimeError):
    """User mutations failed without exposing persistence diagnostics."""


class UserManagementTransaction(Protocol):
    @property
    def user(self) -> User | None: ...

    async def now(self) -> datetime: ...
    async def set_role(self, role: UserRole, now: datetime) -> None: ...
    async def set_status(self, status: UserStatus, now: datetime) -> None: ...
    async def audit(self, event: NewUserSecurityEvent, now: datetime) -> None: ...
    async def lock_families(self) -> tuple[RefreshFamily, ...]: ...
    async def lock_refresh_tokens(
        self, family_ids: tuple[UUID, ...]
    ) -> tuple[StoredRefreshToken, ...]: ...
    async def lock_challenges(self) -> tuple[ResetChallenge, ...]: ...
    async def lock_reset_tokens(
        self, challenge_ids: tuple[UUID, ...]
    ) -> tuple[StoredResetToken, ...]: ...
    async def revoke(self, family_id: UUID, now: datetime) -> None: ...
    async def revoke_challenge(self, challenge_id: UUID, now: datetime) -> None: ...


class UserManagementRepository(Protocol):
    def transaction(self, user_id: UUID) -> AbstractAsyncContextManager[UserManagementTransaction]:
        """Lock the target user; commit on success and roll back on any failure."""
        ...


class UserManagement:
    def __init__(self, repository: UserManagementRepository, authorization: AuthorizationPolicy):
        self._repository = repository
        self._authorization = authorization

    async def change_role(self, principal: HumanPrincipal, user_id: UUID, role: UserRole) -> None:
        self._authorization.authorize(principal, AuthorizationAction.MANAGE_USER_ROLES)
        async with self._repository.transaction(user_id) as tx:
            user = tx.user
            if user is None:
                raise UserNotFound
            self._authorization.authorize_role_change(
                principal, target_user_id=user.id, target_role=user.role, new_role=role
            )
            if user.role == role:
                return
            now = await tx.now()
            await tx.set_role(role, now)
            await tx.audit(
                NewUserSecurityEvent(
                    event_type=SecurityEventType.USER_ROLE_CHANGED,
                    subject_user_id=user.id,
                    initiator_type=SecurityEventInitiatorType.AUTHENTICATED_USER,
                    initiator_user_id=principal.user_id,
                    initiator_role=principal.role,
                    previous_role=user.role,
                    new_role=role,
                ),
                now,
            )

    async def change_status(
        self, principal: HumanPrincipal, user_id: UUID, status: UserStatus
    ) -> None:
        self._authorization.authorize(principal, AuthorizationAction.MANAGE_USER_STATUS)
        if principal.user_id == user_id:
            raise AuthorizationDenied
        async with self._repository.transaction(user_id) as tx:
            user = tx.user
            if user is None:
                raise UserNotFound
            if user.status == status:
                return
            families: tuple[RefreshFamily, ...] = ()
            challenges: tuple[ResetChallenge, ...] = ()
            if status == UserStatus.DISABLED:
                families = await tx.lock_families()
                await tx.lock_refresh_tokens(tuple(f.id for f in families))
                challenges = await tx.lock_challenges()
                await tx.lock_reset_tokens(tuple(c.id for c in challenges))
            now = await tx.now()
            for family in families:
                await tx.revoke(family.id, now)
            for challenge in challenges:
                await tx.revoke_challenge(challenge.id, now)
            await tx.set_status(status, now)
            await tx.audit(
                NewUserSecurityEvent(
                    event_type=SecurityEventType.USER_DISABLED
                    if status == UserStatus.DISABLED
                    else SecurityEventType.USER_ENABLED,
                    subject_user_id=user.id,
                    initiator_type=SecurityEventInitiatorType.AUTHENTICATED_USER,
                    initiator_user_id=principal.user_id,
                    initiator_role=principal.role,
                    previous_status=user.status,
                    new_status=status,
                ),
                now,
            )
