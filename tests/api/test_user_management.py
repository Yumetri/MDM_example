from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from httpx import ASGITransport, AsyncClient

from mdm.api.admin_users import admin_user_error_handler
from mdm.api.errors import authorization_denied_handler, validation_error_handler
from mdm.api.user_management import (
    build_user_management_router,
    user_management_unavailable_handler,
)
from mdm.application.admin_users import UserNotFound
from mdm.application.auth import HumanPrincipal
from mdm.application.authorization import AuthorizationDenied, AuthorizationPolicy
from mdm.application.user_management import UserManagement, UserManagementUnavailable
from mdm.domain.auth import DisplayName, EmailAddress, User, UserRole, UserStatus
from mdm.main import create_app

pytestmark = pytest.mark.api
PATH = f"/api/v1/admin/users/{UUID(int=2)}"


@pytest.mark.parametrize(("action", "value"), [("role", "ADMIN"), ("status", "DISABLED")])
async def test_management_routes_require_bearer_authentication(action, value):
    async with AsyncClient(
        transport=ASGITransport(app=create_app()), base_url="http://test"
    ) as client:
        response = await client.put(f"{PATH}/{action}", json={action: value})
    assert response.status_code == 401
    assert response.json()["code"] == "INVALID_ACCESS_TOKEN"


NOW = datetime(2026, 9, 19, tzinfo=UTC)


class Repository:
    def __init__(self):
        self.tx = AsyncMock()
        self.tx.user = User(
            UUID(int=2),
            EmailAddress("target@example.com"),
            DisplayName("대상"),
            "hash",
            UserRole.USER,
            UserStatus.ACTIVE,
            NOW,
            NOW,
        )
        self.tx.now.return_value = NOW
        self.tx.lock_families.return_value = ()
        self.tx.lock_challenges.return_value = ()
        self.calls = 0
        self.unavailable = False

    @asynccontextmanager
    async def transaction(self, user_id):
        self.calls += 1
        if self.unavailable:
            raise UserManagementUnavailable("secret SQL password")
        yield self.tx


def application(role=UserRole.SUPER_ADMIN, actor_id=None):
    repo = Repository()
    policy = AuthorizationPolicy()
    app = FastAPI()
    app.include_router(
        build_user_management_router(
            use_cases=UserManagement(repo, policy),
            authorization=policy,
            principal_dependency=lambda: HumanPrincipal(actor_id or UUID(int=1), role),
        )
    )
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.add_exception_handler(AuthorizationDenied, authorization_denied_handler)
    app.add_exception_handler(UserNotFound, admin_user_error_handler)
    app.add_exception_handler(UserManagementUnavailable, user_management_unavailable_handler)
    return app, repo


@pytest.mark.parametrize(("action", "value"), [("role", "ADMIN"), ("status", "DISABLED")])
async def test_success_is_bodyless_204_and_uses_verified_actor(action, value):
    app, repo = application()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.put(
            f"{PATH}/{action}",
            json={action: value},
            params={"actor_id": str(UUID(int=99)), "actor_role": "SYSTEM"},
            headers={
                "X-Actor-Id": str(UUID(int=99)),
                "X-Actor-Role": "USER",
                "X-Actor-Kind": "SYSTEM",
            },
        )
    assert response.status_code == 204 and response.content == b""
    audit = repo.tx.audit.call_args.args[0]
    assert audit.initiator_user_id == UUID(int=1)
    assert audit.initiator_role == UserRole.SUPER_ADMIN


@pytest.mark.parametrize(
    ("role", "action", "value"),
    [
        (UserRole.USER, "role", "ADMIN"),
        (UserRole.USER, "status", "DISABLED"),
        (UserRole.ADMIN, "status", "DISABLED"),
    ],
)
async def test_forbidden_actor_cannot_use_spoofed_headers_or_query(role, action, value):
    app, repo = application(role)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.put(
            f"{PATH}/{action}",
            json={action: value},
            params={"actor_role": "SUPER_ADMIN"},
            headers={"X-Actor-Role": "SUPER_ADMIN", "X-Actor-Kind": "SYSTEM"},
        )
    assert response.status_code == 403
    assert response.json()["code"] == "AUTHORIZATION_DENIED"
    assert repo.calls == 0


@pytest.mark.parametrize(
    ("action", "body"),
    [
        ("role", {}),
        ("role", {"role": "SYSTEM"}),
        ("role", {"role": None}),
        ("role", {"role": "ADMIN", "actor_id": str(UUID(int=1))}),
        ("status", {}),
        ("status", {"status": "PENDING"}),
        ("status", {"status": 1}),
        ("status", {"status": "ACTIVE", "actor_kind": "SYSTEM"}),
    ],
)
async def test_invalid_body_is_422_and_never_starts_transaction(action, body):
    app, repo = application()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.put(f"{PATH}/{action}", json=body)
    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"
    assert response.headers["content-type"] == "application/problem+json"
    assert repo.calls == 0


@pytest.mark.parametrize("action", ["role", "status"])
@pytest.mark.parametrize("failure", ["missing", "database", "self"])
async def test_errors_are_rfc9457_and_do_not_leak_persistence_details(action, failure):
    app, repo = application(actor_id=UUID(int=2) if failure == "self" else UUID(int=1))
    if failure == "missing":
        repo.tx.user = None
    repo.unavailable = failure == "database"
    value = "ADMIN" if action == "role" else "DISABLED"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.put(f"{PATH}/{action}", json={action: value})
    expected = {
        "missing": (404, "USER_NOT_FOUND"),
        "database": (503, "SERVICE_UNAVAILABLE"),
        "self": (403, "AUTHORIZATION_DENIED"),
    }[failure]
    assert (response.status_code, response.json()["code"]) == expected
    assert response.headers["content-type"] == "application/problem+json"
    assert "secret" not in response.text and "password" not in response.text


async def test_admin_peer_noop_is_403():
    app, repo = application(UserRole.ADMIN)
    repo.tx.user = replace(repo.tx.user, role=UserRole.ADMIN)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.put(f"{PATH}/role", json={"role": "ADMIN"})
    assert response.status_code == 403
    repo.tx.set_role.assert_not_awaited()


def test_production_openapi_documents_management_without_email_or_role_catalog():
    schema = create_app().openapi()
    paths = schema["paths"]
    for action in ("role", "status"):
        op = paths[f"/api/v1/admin/users/{{user_id}}/{action}"]["put"]
        assert op["operationId"] == f"admin_change_user_{action}"
        assert op["security"] == [{"BearerAuth": []}]
        assert "content" not in op["responses"]["204"]
        assert {"401", "403", "404", "422", "503"} <= op["responses"].keys()
    assert "/api/v1/admin/roles" not in paths
    assert "/api/v1/admin/users/{user_id}/email" not in paths
