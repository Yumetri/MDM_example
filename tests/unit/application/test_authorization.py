from typing import cast
from uuid import UUID

import pytest

from mdm.application.auth import HumanPrincipal
from mdm.application.authorization import (
    AuthorizationAction,
    AuthorizationDenied,
    AuthorizationPolicy,
)
from mdm.domain.auth import UserRole

ACTOR_ID = UUID("018f3f0e-7b2a-7e8f-9f62-9876543210ab")
OTHER_USER_ID = UUID("018f3f0e-7b2a-7e8f-9f62-1234567890ab")


@pytest.mark.unit
@pytest.mark.parametrize(
    ("role", "allowed_actions"),
    [
        (
            UserRole.USER,
            {
                AuthorizationAction.READ_DATA,
                AuthorizationAction.SUBMIT_CREATE_REQUEST,
                AuthorizationAction.SUBMIT_REFERENCE_UPDATE_REQUEST,
                AuthorizationAction.SUBMIT_DELETE_REQUEST,
            },
        ),
        (
            UserRole.ADMIN,
            {
                AuthorizationAction.READ_DATA,
                AuthorizationAction.MUTATE_DATA,
                AuthorizationAction.REVIEW_CHANGE_REQUEST,
                AuthorizationAction.READ_TOMBSTONE,
                AuthorizationAction.READ_AUDIT_LOG,
                AuthorizationAction.MANAGE_USER_ROLES,
            },
        ),
        (
            UserRole.SUPER_ADMIN,
            {
                AuthorizationAction.READ_DATA,
                AuthorizationAction.MUTATE_DATA,
                AuthorizationAction.REVIEW_CHANGE_REQUEST,
                AuthorizationAction.READ_TOMBSTONE,
                AuthorizationAction.READ_AUDIT_LOG,
                AuthorizationAction.MANAGE_USER_ROLES,
            },
        ),
    ],
)
def test_human_role_action_matrix_matches_the_authorization_contract(
    role: UserRole,
    allowed_actions: set[AuthorizationAction],
) -> None:
    policy = AuthorizationPolicy()
    principal = HumanPrincipal(user_id=ACTOR_ID, role=role)

    for action in AuthorizationAction:
        if action in allowed_actions:
            assert policy.authorize(principal, action) is principal
        else:
            with pytest.raises(AuthorizationDenied):
                policy.authorize(principal, action)


@pytest.mark.unit
def test_policy_fails_closed_for_a_non_human_role_value() -> None:
    principal = HumanPrincipal(user_id=ACTOR_ID, role=cast(UserRole, "SYSTEM"))

    with pytest.raises(AuthorizationDenied):
        AuthorizationPolicy().authorize(principal, AuthorizationAction.READ_DATA)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("actor_role", "target_role", "new_role"),
    [
        (UserRole.ADMIN, UserRole.USER, UserRole.USER),
        (UserRole.ADMIN, UserRole.USER, UserRole.ADMIN),
        (UserRole.SUPER_ADMIN, UserRole.USER, UserRole.USER),
        (UserRole.SUPER_ADMIN, UserRole.USER, UserRole.ADMIN),
        (UserRole.SUPER_ADMIN, UserRole.USER, UserRole.SUPER_ADMIN),
        (UserRole.SUPER_ADMIN, UserRole.ADMIN, UserRole.USER),
        (UserRole.SUPER_ADMIN, UserRole.ADMIN, UserRole.ADMIN),
        (UserRole.SUPER_ADMIN, UserRole.ADMIN, UserRole.SUPER_ADMIN),
    ],
)
def test_role_change_allows_only_lower_targets_up_to_the_actor_role(
    actor_role: UserRole,
    target_role: UserRole,
    new_role: UserRole,
) -> None:
    principal = HumanPrincipal(user_id=ACTOR_ID, role=actor_role)

    assert (
        AuthorizationPolicy().authorize_role_change(
            principal,
            target_user_id=OTHER_USER_ID,
            target_role=target_role,
            new_role=new_role,
        )
        is principal
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("actor_role", "target_role", "new_role"),
    [
        (UserRole.USER, UserRole.USER, UserRole.USER),
        (UserRole.USER, UserRole.USER, UserRole.ADMIN),
        (UserRole.ADMIN, UserRole.ADMIN, UserRole.USER),
        (UserRole.ADMIN, UserRole.SUPER_ADMIN, UserRole.USER),
        (UserRole.ADMIN, UserRole.USER, UserRole.SUPER_ADMIN),
        (UserRole.SUPER_ADMIN, UserRole.SUPER_ADMIN, UserRole.USER),
    ],
)
def test_role_change_rejects_peer_higher_and_above_actor_changes(
    actor_role: UserRole,
    target_role: UserRole,
    new_role: UserRole,
) -> None:
    principal = HumanPrincipal(user_id=ACTOR_ID, role=actor_role)

    with pytest.raises(AuthorizationDenied):
        AuthorizationPolicy().authorize_role_change(
            principal,
            target_user_id=OTHER_USER_ID,
            target_role=target_role,
            new_role=new_role,
        )


@pytest.mark.unit
@pytest.mark.parametrize("actor_role", [UserRole.ADMIN, UserRole.SUPER_ADMIN])
def test_role_change_rejects_self_even_if_the_token_role_is_stale(
    actor_role: UserRole,
) -> None:
    principal = HumanPrincipal(user_id=ACTOR_ID, role=actor_role)

    with pytest.raises(AuthorizationDenied):
        AuthorizationPolicy().authorize_role_change(
            principal,
            target_user_id=ACTOR_ID,
            target_role=UserRole.USER,
            new_role=UserRole.USER,
        )
