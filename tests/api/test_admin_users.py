from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from httpx import ASGITransport, AsyncClient

from mdm.api.admin_users import (
    AdminUserListResponse,
    AdminUserResponse,
    admin_user_error_handler,
    build_admin_user_router,
)
from mdm.api.errors import authorization_denied_handler, validation_error_handler
from mdm.application.admin_users import (
    AdminUserQueries,
    UserNotFound,
    UserPage,
    UserQueryUnavailable,
    UserQueryValidationError,
    UserSummary,
)
from mdm.application.auth import HumanPrincipal
from mdm.application.authorization import AuthorizationDenied, AuthorizationPolicy
from mdm.domain.auth import UserRole, UserStatus
from mdm.main import create_app

pytestmark = pytest.mark.api
PATH = "/api/v1/admin/users"


@pytest.mark.parametrize("path", [PATH, f"{PATH}/{UUID(int=1)}"])
async def test_admin_user_queries_require_bearer_authentication_in_production(path):
    async with AsyncClient(
        transport=ASGITransport(app=create_app()), base_url="http://test"
    ) as client:
        response = await client.get(path)
    assert response.status_code == 401
    assert response.json()["code"] == "INVALID_ACCESS_TOKEN"


class Repository:
    def __init__(self):
        self.calls = []
        self.missing = False
        self.unavailable = False
        self.items = tuple(
            UserSummary(
                UUID(int=i),
                f"reader{i}@example.com",
                "사용자",
                UserRole.USER,
                UserStatus.ACTIVE,
                datetime(2026, 9, 17, tzinfo=UTC),
                datetime(2026, 9, 17, tzinfo=UTC),
            )
            for i in (4, 3, 2, 1)
        )

    async def list_users(self, *, visible_role, filters, after, limit):
        self.calls.append((visible_role, filters, after, limit))
        if self.unavailable:
            raise UserQueryUnavailable("password=secret SELECT internal_table")
        items = tuple(item for item in self.items if after is None or item.id < after.id)
        return UserPage(items[:limit], len(items) > limit)

    async def get_user(self, user_id, *, visible_role):
        self.calls.append((user_id, visible_role))
        if self.unavailable:
            raise UserQueryUnavailable("password=secret SELECT internal_table")
        return None if self.missing else replace(self.items[0], id=user_id)


def application(role=UserRole.ADMIN):
    repository = Repository()
    authorization = AuthorizationPolicy()
    app = FastAPI()
    app.include_router(
        build_admin_user_router(
            use_cases=AdminUserQueries(repository, authorization),
            authorization=authorization,
            principal_dependency=lambda: HumanPrincipal(UUID(int=1), role),
        )
    )
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.add_exception_handler(AuthorizationDenied, authorization_denied_handler)
    for error in (UserNotFound, UserQueryUnavailable, UserQueryValidationError):
        app.add_exception_handler(error, admin_user_error_handler)
    return app, repository


@pytest.mark.parametrize("path", [PATH, f"{PATH}/{UUID(int=9)}"])
async def test_user_role_spoofing_cannot_reach_the_repository(path):
    app, repository = application(UserRole.USER)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        result = await client.get(
            path,
            params={"role": "SUPER_ADMIN"},
            headers={
                "X-Actor-Role": "SUPER_ADMIN",
                "X-Actor-Kind": "SYSTEM",
            },
        )
    assert result.status_code == 403
    assert result.json()["code"] == "AUTHORIZATION_DENIED"
    assert repository.calls == []


@pytest.mark.parametrize(
    "params",
    [
        {"email": ""},
        {"email": "invalid"},
        {"role": "SYSTEM"},
        {"status": "PENDING"},
        {"limit": "0"},
        {"limit": "101"},
        {"limit": "1.5"},
        {"cursor": ""},
        {"cursor": "invalid"},
        {"cursor": "x" * 513},
        {"name": "홍길동"},
        {"actor_role": "SUPER_ADMIN"},
    ],
)
async def test_invalid_query_returns_422_without_a_repository_call(params):
    app, repository = application()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        result = await client.get(PATH, params=params)
    assert result.status_code == 422
    assert result.json()["code"] == "VALIDATION_ERROR"
    assert result.headers["content-type"] == "application/problem+json"
    assert repository.calls == []


@pytest.mark.parametrize(
    "changed",
    [
        {"email": "other@example.com"},
        {"role": "ADMIN"},
        {"status": "DISABLED"},
    ],
)
async def test_cursor_rejects_changed_filters_but_allows_limit_and_normalized_email(changed):
    app, repository = application()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        params = {"email": " Reader@EXAMPLE.com ", "role": "USER", "status": "ACTIVE", "limit": "1"}
        first = await client.get(PATH, params=params)
        assert first.status_code == 200, first.text
        cursor = first.json()["next_cursor"]
        params.update(email="reader@example.com", cursor=cursor, limit="2")
        second = await client.get(PATH, params=params)
        assert second.status_code == 200, second.text
        assert len(second.json()["items"]) == 2
        params.update(changed)
        rejected = await client.get(PATH, params=params)
    assert rejected.status_code == 422
    assert len(repository.calls) == 2


async def test_invalid_uuid_does_not_reach_repository():
    app, repository = application()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        result = await client.get(f"{PATH}/invalid")
    assert result.status_code == 422
    assert repository.calls == []


async def test_missing_detail_has_the_approved_problem_response():
    app, repository = application()
    repository.missing = True
    path = f"{PATH}/{UUID(int=9)}"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        result = await client.get(path)
    assert result.status_code == 404
    assert result.json() == {
        "type": "/problems/user-not-found",
        "title": "사용자를 찾을 수 없음",
        "status": 404,
        "detail": "조회할 수 있는 사용자가 없습니다.",
        "code": "USER_NOT_FOUND",
        "instance": path,
    }


@pytest.mark.parametrize("path", [PATH, f"{PATH}/{UUID(int=9)}"])
async def test_query_failure_hides_internal_details(path):
    app, repository = application()
    repository.unavailable = True
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        result = await client.get(path)
    assert result.status_code == 503
    assert result.json()["code"] == "SERVICE_UNAVAILABLE"
    assert "secret" not in result.text and "internal_table" not in result.text


@pytest.mark.parametrize("timestamp", ["2026-09-17T00:00:00", "9999-12-31T23:59:59-01:00"])
async def test_cursor_rejects_naive_or_unrepresentable_timestamps(timestamp):
    import base64
    import json

    app, repository = application()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        first = await client.get(PATH, params={"limit": 1})
        encoded = first.json()["next_cursor"]
        data = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
        data["t"] = timestamp
        encoded = base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")
        result = await client.get(PATH, params={"cursor": encoded})
    assert result.status_code == 422
    assert len(repository.calls) == 1


def test_openapi_exposes_only_public_projection_and_no_role_listing():
    schema = create_app().openapi()
    fields = {"id", "email", "name", "role", "status", "created_at", "updated_at"}
    assert set(schema["components"]["schemas"]["AdminUserResponse"]["properties"]) == fields
    assert set(schema["components"]["schemas"]["AdminUserListResponse"]["properties"]) == {
        "items",
        "next_cursor",
    }
    for path, operation_id in [
        (PATH, "admin_list_users"),
        (f"{PATH}/{{user_id}}", "admin_get_user"),
    ]:
        assert set(schema["paths"][path]) == {"get"}
        operation = schema["paths"][path]["get"]
        assert operation["operationId"] == operation_id
        assert operation["security"]
        assert {"200", "401", "403", "422", "503"} <= operation["responses"].keys()
        for status in ("404", "422", "503"):
            if status in operation["responses"]:
                instance = operation["responses"][status]["content"]["application/problem+json"][
                    "example"
                ]["instance"]
                assert "{" not in instance and "}" not in instance
        content = operation["responses"]["200"]["content"]["application/json"]
        if path == PATH:
            for example in content["examples"].values():
                AdminUserListResponse.model_validate(example["value"])
        else:
            assert "404" in operation["responses"]
            AdminUserResponse.model_validate(content["example"])
    assert not any(path.endswith("/roles") for path in schema["paths"])
