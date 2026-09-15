from datetime import UTC, datetime
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from httpx import ASGITransport, AsyncClient

from mdm.api.audit_logs import build_audit_log_router
from mdm.api.errors import (
    authorization_denied_handler,
    dimension_validation_error_handler,
    validation_error_handler,
)
from mdm.application.audit_logs import (
    AuditLogPage,
    AuditSource,
    DimensionAuditLog,
    ListAuditLogs,
)
from mdm.application.auth import HumanPrincipal
from mdm.application.authorization import AuthorizationDenied, AuthorizationPolicy
from mdm.domain.audit import ActorKind, DimensionOperation
from mdm.domain.auth import UserRole
from mdm.domain.dimensions import DimensionValidationError
from mdm.main import create_app

pytestmark = pytest.mark.api
BASE = "/api/v1/admin"
COLLECTIONS = (
    "companies",
    "models",
    "brands",
    "countries",
    "categories",
    "years",
    "networks",
    "memories",
)
PATHS = [
    *(f"{BASE}/dimension-logs/{name}" for name in COLLECTIONS),
    f"{BASE}/master-code-logs",
    f"{BASE}/change-sets/{UUID(int=10)}/logs",
]


class Repository:
    def __init__(self):
        self.calls = []

    async def list_logs(self, query, *, after, limit):
        self.calls.append((query, after, limit))
        items = tuple(
            DimensionAuditLog(
                id=UUID(int=i),
                source_type=query.source or AuditSource.COMPANY,
                change_set_id=query.change_set_id or UUID(int=10),
                changed_at=datetime(2026, 9, 15, tzinfo=UTC),
                actor_kind=ActorKind.HUMAN,
                actor_id=str(UUID(int=1)),
                actor_role=UserRole.ADMIN,
                reason=None,
                dimension_id=UUID(int=20),
                dimension_version=2,
                operation=DimensionOperation.DELETE,
                field_name="DELETED",
                old_value=False,
                new_value=True,
            )
            for i in (4, 3, 2, 1)
            if after is None or UUID(int=i) < after.id
        )
        return AuditLogPage(items[:limit], len(items) > limit)


def application(role=UserRole.ADMIN):
    repository = Repository()
    policy = AuthorizationPolicy()
    app = FastAPI()
    app.include_router(
        build_audit_log_router(
            use_case=ListAuditLogs(repository, policy),
            authorization=policy,
            principal_dependency=lambda: HumanPrincipal(UUID(int=1), role),
        )
    )
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.add_exception_handler(DimensionValidationError, dimension_validation_error_handler)
    app.add_exception_handler(AuthorizationDenied, authorization_denied_handler)
    return app, repository


@pytest.mark.parametrize("path", PATHS)
async def test_all_audit_endpoints_require_authentication_in_composition(path):
    async with AsyncClient(
        transport=ASGITransport(app=create_app()), base_url="http://test"
    ) as client:
        response = await client.get(path)
    assert response.status_code == 401


@pytest.mark.parametrize("path", PATHS)
async def test_user_cannot_read_audit_logs_even_with_role_query_spoofing(path):
    app, repo = application(UserRole.USER)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(path, params={"actor_role": "SUPER_ADMIN"})
    assert response.status_code == 403
    assert repo.calls == []


@pytest.mark.parametrize(
    "params",
    [
        {"changed_from": "2026-09-15T00:00:00"},
        {"changed_from": "9999-12-31T23:59:59-23:59"},
        {"changed_before": "0001-01-01T00:00:00+23:59"},
        {"changed_from": "2026-09-15T00:00:00Z", "changed_before": "2026-09-15T00:00:00Z"},
        {"changed_before": "1789430400"},
        {"limit": "0"},
        {"limit": "101"},
        {"cursor": ""},
        {"cursor": "broken"},
        {"operation": "RECOMPOSE"},
        {"field_name": "AMOUNT"},
        {"actor_id": ""},
        {"actor_id": "invalid\x00actor"},
        {"dimension_id": "bad"},
    ],
)
async def test_invalid_queries_are_problem_details_before_repository(params):
    app, repo = application()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(PATHS[0], params=params)
    assert response.status_code == 422, response.text
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["code"] == "VALIDATION_ERROR"
    assert repo.calls == []


async def test_cursor_binds_route_filters_but_allows_limit_and_equivalent_timezone():
    app, repo = application()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        params = {"limit": "1", "changed_from": "2026-09-15T09:00:00+09:00"}
        first = await client.get(PATHS[0], params=params)
        assert first.status_code == 200, first.text
        assert first.json()["items"][0]["old_value"] is False
        cursor = first.json()["next_cursor"]
        params.update(cursor=cursor, changed_from="2026-09-15T00:00:00Z", limit="2")
        second = await client.get(PATHS[0], params=params)
        assert second.status_code == 200, second.text
        assert len(second.json()["items"]) == 2
        assert (await client.get(PATHS[1], params=params)).status_code == 422
        params["actor_role"] = "USER"
        assert (await client.get(PATHS[0], params=params)).status_code == 422
    assert len(repo.calls) == 2


async def test_combined_endpoint_rejects_filters_and_cross_change_set_cursor():
    app, repo = application()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (
            await client.get(PATHS[-1], params={"actor_id": str(UUID(int=1))})
        ).status_code == 422
        result = await client.get(PATHS[-1], params={"limit": 1})
        assert result.status_code == 200, result.text
        other = f"{BASE}/change-sets/{UUID(int=11)}/logs"
        assert (
            await client.get(other, params={"cursor": result.json()["next_cursor"]})
        ).status_code == 422
    assert len(repo.calls) == 1


def test_openapi_audit_examples_match_typed_response_contracts():
    from pydantic import TypeAdapter

    from mdm.api.audit_log_schemas import AuditLogListResponse, AuditLogResponse

    schema = create_app().openapi()
    adapter = TypeAdapter(AuditLogListResponse[AuditLogResponse])
    for path in [*PATHS[:-1], f"{BASE}/change-sets/{{change_set_id}}/logs"]:
        operation = schema["paths"][path]["get"]
        assert set(operation["responses"]) >= {"200", "401", "403", "422", "503"}
        examples = operation["responses"]["200"]["content"]["application/json"]["examples"]
        for example in examples.values():
            adapter.validate_python(example["value"])
            assert "next_cursor" in example["value"]
        assert set(schema["paths"][path]) == {"get"}


async def test_audit_unavailability_hides_internal_diagnostics():
    from mdm.api.audit_logs import audit_log_unavailable_handler
    from mdm.application.audit_logs import AuditLogRepositoryUnavailable

    app, repo = application()
    app.add_exception_handler(AuditLogRepositoryUnavailable, audit_log_unavailable_handler)

    async def unavailable(query, *, after, limit):
        raise AuditLogRepositoryUnavailable("password=secret SELECT * FROM internal_table")

    repo.list_logs = unavailable
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(PATHS[0])
    assert response.status_code == 503
    assert response.json()["code"] == "SERVICE_UNAVAILABLE"
    assert "secret" not in response.text and "internal_table" not in response.text


async def test_cursor_with_unrepresentable_utc_timestamp_is_validation_error():
    import base64
    import json

    app, _ = application()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        first = await client.get(PATHS[0], params={"limit": 1})
        original = first.json()["next_cursor"]
        data = json.loads(base64.urlsafe_b64decode(original + "=" * (-len(original) % 4)))
        data["t"] = "9999-12-31T23:59:59-01:00"
        malformed = base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")
        response = await client.get(PATHS[0], params={"cursor": malformed})
        assert response.status_code == 422
        assert response.json()["code"] == "VALIDATION_ERROR"
