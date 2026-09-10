from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from uuid import UUID

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from mdm.api.auth import build_human_principal_dependency
from mdm.api.memory_dimensions import build_memory_router
from mdm.application.audit import HumanMutationAuditFactory
from mdm.application.auth import (
    AccessTokenClaims,
    AuthenticateHumanPrincipal,
    HumanPrincipal,
    InvalidAccessToken,
    OperationalEvent,
)
from mdm.application.authorization import AuthorizationPolicy
from mdm.application.memory_dimensions import (
    CreateMemory,
    GetMemory,
    ListMemories,
    MemoryDimensionCursor,
    MemoryDimensionPage,
    MemoryRepository,
)
from mdm.domain.audit import MutationAuditMetadata
from mdm.domain.auth import UserRole
from mdm.domain.dimensions import Dimension, DimensionCode, MemoryUnit, MemoryValue
from mdm.main import create_app

USER_ID = UUID("01890f7c-8abc-7def-8abc-111111111111")
MEMORY_ID = UUID("01890f7c-8abc-7def-8abc-222222222222")
JTI = UUID("123e4567-e89b-42d3-a456-426614174000")
CHANGE_SET_ID = UUID("01890f7c-8abc-7def-8abc-333333333333")
NOW = datetime(2033, 5, 18, 3, 33, 20, tzinfo=UTC)


class HealthyReadinessCheck:
    async def execute(self) -> None:
        return None


class RoleVerifier:
    def verify(self, token: str, *, now: int) -> AccessTokenClaims:
        try:
            role = UserRole(token)
        except ValueError:
            raise InvalidAccessToken from None
        return AccessTokenClaims(
            user_id=USER_ID,
            role=role,
            issued_at=now - 1,
            expires_at=now + 899,
            jti=JTI,
        )


class NullEventSink:
    def emit(self, event: OperationalEvent) -> None:
        del event


class FakeMemoryRepository(MemoryRepository):
    def __init__(self) -> None:
        self.memory = Dimension(
            id=MEMORY_ID,
            code=DimensionCode("MEM128"),
            value=MemoryValue(amount=128, unit=MemoryUnit.GB),
            version=1,
            created_at=NOW,
            updated_at=NOW,
            deleted_at=None,
        )
        self.created: tuple[DimensionCode, MemoryValue, MutationAuditMetadata] | None = None
        self.list_after: MemoryDimensionCursor | None = None

    async def create(self, code, value, audit):
        self.created = (code, value, audit)
        return self.memory

    async def get_active(self, dimension_id):
        return self.memory

    async def list_active(self, *, after, limit):
        self.list_after = after
        return MemoryDimensionPage(items=(self.memory,), has_more=after is None)


def build_application(repository: FakeMemoryRepository) -> FastAPI:
    application = create_app(readiness_check=HealthyReadinessCheck())
    authentication = AuthenticateHumanPrincipal(RoleVerifier(), NullEventSink(), clock=lambda: NOW)
    principal_dependency: Callable[..., HumanPrincipal] = build_human_principal_dependency(
        authentication
    )
    policy = AuthorizationPolicy()
    application.include_router(
        build_memory_router(
            create_memory=CreateMemory(
                repository,
                policy,
                HumanMutationAuditFactory(change_set_ids=lambda: CHANGE_SET_ID),
            ),
            get_memory=GetMemory(repository, policy),
            list_memories=ListMemories(repository, policy),
            principal_dependency=principal_dependency,
            authorization=policy,
        )
    )
    application.openapi_schema = None
    return application


@asynccontextmanager
async def client_for(repository: FakeMemoryRepository) -> AsyncGenerator[AsyncClient]:
    transport = ASGITransport(app=build_application(repository), raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest.mark.api
async def test_admin_creates_normalized_memory_and_receives_derived_capacity() -> None:
    repository = FakeMemoryRepository()

    async with client_for(repository) as client:
        response = await client.post(
            "/dimensions/memories",
            headers={"Authorization": "Bearer ADMIN"},
            json={
                "code": "mem128",
                "value": {"amount": 128, "unit": " gb "},
                "reason": "등록",
            },
        )

    assert response.status_code == 201
    assert response.headers["etag"] == '"1"'
    assert response.json()["value"] == {
        "amount": 128,
        "unit": "GB",
        "capacity_mb": 128_000,
    }
    assert repository.created is not None
    assert repository.created[1] == MemoryValue(amount=128, unit=MemoryUnit.GB)


@pytest.mark.api
async def test_capacity_input_and_actor_spoofing_are_rejected() -> None:
    repository = FakeMemoryRepository()

    async with client_for(repository) as client:
        capacity = await client.post(
            "/dimensions/memories",
            headers={"Authorization": "Bearer ADMIN"},
            json={
                "code": "MEM128",
                "value": {"amount": 128, "unit": "GB", "capacity_mb": 128_000},
            },
        )
        actor = await client.post(
            "/dimensions/memories",
            headers={"Authorization": "Bearer ADMIN"},
            json={
                "code": "MEM128",
                "value": {"amount": 128, "unit": "GB"},
                "actor_kind": "SYSTEM",
            },
        )

    assert capacity.status_code == 422
    assert capacity.json()["violations"][0]["field"] == "body.value.capacity_mb"
    assert actor.status_code == 422
    assert actor.json()["violations"][0]["field"] == "body.actor_kind"
    assert repository.created is None


@pytest.mark.api
async def test_user_cannot_create_memory_but_can_read_and_page() -> None:
    repository = FakeMemoryRepository()

    async with client_for(repository) as client:
        denied = await client.post(
            "/dimensions/memories",
            headers={"Authorization": "Bearer USER"},
            json={"code": "MEM128", "value": {"amount": 128, "unit": "GB"}},
        )
        detail = await client.get(
            f"/dimensions/memories/{MEMORY_ID}", headers={"Authorization": "Bearer USER"}
        )
        first = await client.get(
            "/dimensions/memories?limit=1", headers={"Authorization": "Bearer USER"}
        )
        second = await client.get(
            "/dimensions/memories",
            params={"limit": 1, "cursor": first.json()["next_cursor"]},
            headers={"Authorization": "Bearer USER"},
        )

    assert denied.status_code == 403
    assert detail.status_code == 200
    assert detail.headers["etag"] == '"1"'
    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["next_cursor"] is None
    assert repository.list_after == MemoryDimensionCursor(created_at=NOW, id=MEMORY_ID)


@pytest.mark.api
def test_memory_openapi_exposes_nested_read_only_capacity_contract() -> None:
    schema = create_app().openapi()
    collection = schema["paths"]["/dimensions/memories"]
    detail = schema["paths"]["/dimensions/memories/{dimension_id}"]

    assert collection["post"]["operationId"] == "create_memory_dimension"
    assert collection["get"]["operationId"] == "list_memory_dimensions"
    assert detail["get"]["operationId"] == "get_memory_dimension"
    input_value = schema["components"]["schemas"]["MemoryValueInput"]
    output_value = schema["components"]["schemas"]["MemoryValueResponse"]
    assert set(input_value["properties"]) == {"amount", "unit"}
    assert input_value["additionalProperties"] is False
    assert set(output_value["properties"]) == {"amount", "unit", "capacity_mb"}
    assert input_value["properties"]["unit"]["pattern"]
    assert output_value["properties"]["unit"]["enum"] == ["MB", "GB", "TB", "PB"]
    assert output_value["properties"]["amount"]["minimum"] == 1
    assert output_value["properties"]["amount"]["maximum"] == 2_147_483_647
    assert schema["components"]["schemas"]["MemoryResponse"]["example"]["deleted_at"] is None
    assert (
        schema["components"]["schemas"]["MemoryListResponse"]["example"]["items"][0]["deleted_at"]
        is None
    )
