from datetime import UTC, datetime
from uuid import UUID

import pytest

from mdm.application.admin_users import (
    AdminUserQueries,
    UserFilters,
    UserPage,
    UserSummary,
)
from mdm.application.auth import HumanPrincipal
from mdm.application.authorization import AuthorizationDenied, AuthorizationPolicy
from mdm.domain.auth import UserRole, UserStatus

pytestmark = pytest.mark.unit


class Repository:
    def __init__(self):
        self.calls = []
        self.item = UserSummary(
            UUID(int=9),
            "reader@example.com",
            "사용자",
            UserRole.USER,
            UserStatus.ACTIVE,
            datetime(2026, 9, 17, tzinfo=UTC),
            datetime(2026, 9, 17, tzinfo=UTC),
        )

    async def list_users(self, *, visible_role, filters, after, limit):
        self.calls.append((visible_role, filters, after, limit))
        return UserPage((self.item,), False)

    async def get_user(self, user_id, *, visible_role):
        self.calls.append((user_id, visible_role))
        return self.item


@pytest.mark.parametrize("role", [UserRole.ADMIN, UserRole.SUPER_ADMIN])
async def test_query_passes_trusted_visibility_to_both_repository_operations(role):
    repository = Repository()
    service = AdminUserQueries(repository, AuthorizationPolicy())
    principal = HumanPrincipal(UUID(int=1), role)
    filters = UserFilters(role=UserRole.SUPER_ADMIN, status=UserStatus.DISABLED)
    page = await service.list_users(principal, filters, after=None, limit=10)
    item = await service.get_user(principal, UUID(int=9))
    visible = UserRole.USER if role == UserRole.ADMIN else None
    assert repository.calls == [(visible, filters, None, 10), (UUID(int=9), visible)]
    assert page.items == (item,)


async def test_user_is_rejected_before_either_repository_operation():
    repository = Repository()
    service = AdminUserQueries(repository, AuthorizationPolicy())
    principal = HumanPrincipal(UUID(int=1), UserRole.USER)
    with pytest.raises(AuthorizationDenied):
        await service.list_users(principal, UserFilters(), after=None, limit=50)
    with pytest.raises(AuthorizationDenied):
        await service.get_user(principal, UUID(int=9))
    assert repository.calls == []


@pytest.mark.parametrize("limit", [0, 101, True, 1.5])
async def test_invalid_page_size_fails_before_repository(limit):
    from mdm.application.admin_users import UserQueryValidationError

    repository = Repository()
    service = AdminUserQueries(repository, AuthorizationPolicy())
    with pytest.raises(UserQueryValidationError):
        await service.list_users(
            HumanPrincipal(UUID(int=1), UserRole.ADMIN), UserFilters(), after=None, limit=limit
        )
    assert repository.calls == []
