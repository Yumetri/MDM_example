"""Framework-independent authorization policy for verified HUMAN principals."""

from enum import StrEnum
from uuid import UUID

from mdm.application.auth import HumanPrincipal
from mdm.domain.auth import UserRole


class AuthorizationDenied(RuntimeError):
    """A verified HUMAN principal is not allowed to perform an action."""


class AuthorizationAction(StrEnum):
    """The stable authorization decisions shared by MDM use cases and API guards."""

    READ_DATA = "READ_DATA"
    SUBMIT_CREATE_REQUEST = "SUBMIT_CREATE_REQUEST"
    SUBMIT_REFERENCE_UPDATE_REQUEST = "SUBMIT_REFERENCE_UPDATE_REQUEST"
    SUBMIT_DELETE_REQUEST = "SUBMIT_DELETE_REQUEST"
    MUTATE_DATA = "MUTATE_DATA"
    REVIEW_CHANGE_REQUEST = "REVIEW_CHANGE_REQUEST"
    READ_TOMBSTONE = "READ_TOMBSTONE"
    READ_AUDIT_LOG = "READ_AUDIT_LOG"
    MANAGE_USER_ROLES = "MANAGE_USER_ROLES"


_USER_ACTIONS = frozenset(
    {
        AuthorizationAction.READ_DATA,
        AuthorizationAction.SUBMIT_CREATE_REQUEST,
        AuthorizationAction.SUBMIT_REFERENCE_UPDATE_REQUEST,
        AuthorizationAction.SUBMIT_DELETE_REQUEST,
    }
)
_ADMIN_ACTIONS = frozenset(
    {
        AuthorizationAction.READ_DATA,
        AuthorizationAction.MUTATE_DATA,
        AuthorizationAction.REVIEW_CHANGE_REQUEST,
        AuthorizationAction.READ_TOMBSTONE,
        AuthorizationAction.READ_AUDIT_LOG,
        AuthorizationAction.MANAGE_USER_ROLES,
    }
)
_ALLOWED_ACTIONS = {
    UserRole.USER: _USER_ACTIONS,
    UserRole.ADMIN: _ADMIN_ACTIONS,
    UserRole.SUPER_ADMIN: _ADMIN_ACTIONS,
}
_ROLE_RANK = {
    UserRole.USER: 0,
    UserRole.ADMIN: 1,
    UserRole.SUPER_ADMIN: 2,
}


class AuthorizationPolicy:
    """Apply the closed HUMAN role matrix without framework or persistence access."""

    def authorize(
        self,
        principal: HumanPrincipal,
        action: AuthorizationAction,
    ) -> HumanPrincipal:
        """Return the trusted principal when its exact role permits the action."""
        if not isinstance(principal.role, UserRole):
            raise AuthorizationDenied
        if action not in _ALLOWED_ACTIONS[principal.role]:
            raise AuthorizationDenied
        return principal

    def authorize_role_change(
        self,
        principal: HumanPrincipal,
        *,
        target_user_id: UUID,
        target_role: UserRole,
        new_role: UserRole,
    ) -> HumanPrincipal:
        """Enforce different-user, lower-target, and actor-role ceiling rules."""
        self.authorize(principal, AuthorizationAction.MANAGE_USER_ROLES)
        if not isinstance(target_role, UserRole) or not isinstance(new_role, UserRole):
            raise AuthorizationDenied
        if target_user_id == principal.user_id:
            raise AuthorizationDenied

        actor_rank = _ROLE_RANK[principal.role]
        if _ROLE_RANK[target_role] >= actor_rank or _ROLE_RANK[new_role] > actor_rank:
            raise AuthorizationDenied
        return principal
