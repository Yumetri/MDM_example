from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from mdm.application.admin_users import UserNotFound
from mdm.application.auth import HumanPrincipal
from mdm.application.authorization import AuthorizationDenied, AuthorizationPolicy
from mdm.application.user_management import UserManagement
from mdm.domain.auth import DisplayName, EmailAddress, User, UserRole, UserStatus

pytestmark = pytest.mark.unit
NOW = datetime(2026, 9, 19, tzinfo=UTC)
ACTOR = HumanPrincipal(UUID(int=1), UserRole.SUPER_ADMIN)
TARGET = User(
    UUID(int=2),
    EmailAddress("target@example.com"),
    DisplayName("대상"),
    "hash",
    UserRole.USER,
    UserStatus.ACTIVE,
    NOW,
    NOW,
)


class Repository:
    def __init__(self, user=TARGET):
        self.tx = AsyncMock()
        self.tx.user = user
        self.tx.now.return_value = NOW
        self.calls = 0

    @asynccontextmanager
    async def transaction(self, user_id):
        self.calls += 1
        yield self.tx


async def test_role_change_records_typed_transition_without_revoking_credentials():
    repo = Repository()
    await UserManagement(repo, AuthorizationPolicy()).change_role(ACTOR, TARGET.id, UserRole.ADMIN)
    repo.tx.set_role.assert_awaited_once_with(UserRole.ADMIN, NOW)
    audit = repo.tx.audit.call_args.args[0]
    assert (audit.previous_role, audit.new_role) == (UserRole.USER, UserRole.ADMIN)
    assert audit.initiator_user_id == ACTOR.user_id
    assert audit.initiator_role == ACTOR.role
    assert audit.subject_user_id == TARGET.id
    repo.tx.lock_families.assert_not_awaited()


@pytest.mark.parametrize("action", ["role", "status"])
async def test_authorized_noop_does_not_change_time_credentials_or_audit(action):
    repo = Repository()
    use_case = UserManagement(repo, AuthorizationPolicy())
    if action == "role":
        await use_case.change_role(ACTOR, TARGET.id, TARGET.role)
    else:
        await use_case.change_status(ACTOR, TARGET.id, TARGET.status)
    repo.tx.now.assert_not_awaited()
    repo.tx.audit.assert_not_awaited()
    repo.tx.lock_families.assert_not_awaited()
    repo.tx.set_role.assert_not_awaited()
    repo.tx.set_status.assert_not_awaited()


async def test_admin_promotion_retry_is_denied_before_noop():
    repo = Repository(replace(TARGET, role=UserRole.ADMIN))
    with pytest.raises(AuthorizationDenied):
        await UserManagement(repo, AuthorizationPolicy()).change_role(
            replace(ACTOR, role=UserRole.ADMIN), TARGET.id, UserRole.ADMIN
        )
    repo.tx.audit.assert_not_awaited()


@pytest.mark.parametrize("action", ["role", "status"])
async def test_missing_user_is_not_found(action):
    repo = Repository(None)
    use_case = UserManagement(repo, AuthorizationPolicy())
    with pytest.raises(UserNotFound):
        if action == "role":
            await use_case.change_role(ACTOR, TARGET.id, UserRole.ADMIN)
        else:
            await use_case.change_status(ACTOR, TARGET.id, UserStatus.DISABLED)


@pytest.mark.parametrize("action", ["role", "status"])
async def test_self_change_is_denied_even_for_noop(action):
    repo = Repository(replace(TARGET, id=ACTOR.user_id, role=ACTOR.role))
    use_case = UserManagement(repo, AuthorizationPolicy())
    with pytest.raises(AuthorizationDenied):
        if action == "role":
            await use_case.change_role(ACTOR, ACTOR.user_id, ACTOR.role)
        else:
            await use_case.change_status(ACTOR, ACTOR.user_id, UserStatus.ACTIVE)
    repo.tx.audit.assert_not_awaited()


@pytest.mark.parametrize("role", [UserRole.USER, UserRole.ADMIN])
async def test_status_change_rejects_non_super_admin_before_database(role):
    repo = Repository()
    with pytest.raises(AuthorizationDenied):
        await UserManagement(repo, AuthorizationPolicy()).change_status(
            replace(ACTOR, role=role), TARGET.id, UserStatus.DISABLED
        )
    assert repo.calls == 0


async def test_user_role_change_rejected_before_database():
    repo = Repository()
    with pytest.raises(AuthorizationDenied):
        await UserManagement(repo, AuthorizationPolicy()).change_role(
            replace(ACTOR, role=UserRole.USER), TARGET.id, UserRole.ADMIN
        )
    assert repo.calls == 0
