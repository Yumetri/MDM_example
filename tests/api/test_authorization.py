from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

import pytest
from fastapi import Depends, FastAPI, status
from httpx import ASGITransport, AsyncClient

from mdm.api.auth import INVALID_ACCESS_TOKEN_RESPONSE, build_human_principal_dependency
from mdm.api.authorization import (
    AUTHORIZATION_DENIED_RESPONSE,
    build_authorization_guard,
)
from mdm.api.openapi import configure_openapi
from mdm.application.auth import (
    AccessTokenClaims,
    AuthenticateHumanPrincipal,
    HumanPrincipal,
    InvalidAccessToken,
    OperationalEvent,
)
from mdm.application.authorization import AuthorizationAction, AuthorizationPolicy
from mdm.domain.auth import UserRole
from mdm.main import create_app

USER_ID = UUID("018f3f0e-7b2a-7e8f-9f62-9876543210ab")
JTI = UUID("123e4567-e89b-42d3-a456-426614174000")
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


def build_authorized_application(action: AuthorizationAction) -> FastAPI:
    application = create_app(readiness_check=HealthyReadinessCheck())
    authenticate = AuthenticateHumanPrincipal(RoleVerifier(), NullEventSink(), clock=lambda: NOW)
    principal_dependency: Callable[..., HumanPrincipal] = build_human_principal_dependency(
        authenticate
    )
    authorization_guard: Callable[..., HumanPrincipal] = build_authorization_guard(
        principal_dependency,
        AuthorizationPolicy(),
        action,
    )

    @application.get(
        "/authorization-probe",
        operation_id="get_authorization_probe",
        response_model=dict[str, str],
        status_code=status.HTTP_200_OK,
        summary="권한 검사 경계 확인",
        description="테스트에서 검증된 HUMAN 역할의 권한 검사 경계를 확인합니다.",
        responses={
            status.HTTP_401_UNAUTHORIZED: INVALID_ACCESS_TOKEN_RESPONSE,
            status.HTTP_403_FORBIDDEN: AUTHORIZATION_DENIED_RESPONSE,
        },
    )
    def authorization_probe(
        principal: Annotated[HumanPrincipal, Depends(authorization_guard)],
    ) -> dict[str, str]:
        return {"user_id": str(principal.user_id), "role": principal.role.value}

    application.openapi_schema = None
    configure_openapi(application)
    return application


@asynccontextmanager
async def client_for(action: AuthorizationAction) -> AsyncGenerator[AsyncClient]:
    application = build_authorized_application(action)
    transport = ASGITransport(app=application, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest.mark.api
async def test_guard_returns_verified_principal_for_an_allowed_role() -> None:
    async with client_for(AuthorizationAction.MUTATE_DATA) as client:
        response = await client.get(
            "/authorization-probe?role=SUPER_ADMIN&actor_kind=SYSTEM",
            headers={
                "Authorization": "Bearer ADMIN",
                "X-Actor-Role": "SUPER_ADMIN",
                "X-Actor-Kind": "SYSTEM",
            },
        )

    assert response.status_code == 200
    assert response.json() == {"user_id": str(USER_ID), "role": "ADMIN"}


@pytest.mark.api
async def test_guard_returns_rfc_9457_403_for_a_denied_role() -> None:
    async with client_for(AuthorizationAction.MUTATE_DATA) as client:
        response = await client.get(
            "/authorization-probe",
            headers={"Authorization": "Bearer USER"},
        )

    assert response.status_code == 403
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json() == {
        "type": "/problems/authorization-denied",
        "title": "권한 없음",
        "status": 403,
        "detail": "현재 역할로는 이 작업을 수행할 수 없습니다.",
        "code": "AUTHORIZATION_DENIED",
        "instance": "/authorization-probe",
    }


@pytest.mark.api
async def test_guard_keeps_authentication_failure_as_401() -> None:
    async with client_for(AuthorizationAction.READ_DATA) as client:
        response = await client.get("/authorization-probe")

    assert response.status_code == 401
    assert response.json()["code"] == "INVALID_ACCESS_TOKEN"


@pytest.mark.api
def test_authorized_operation_documents_bearer_401_and_authorization_403() -> None:
    operation = build_authorized_application(AuthorizationAction.READ_AUDIT_LOG).openapi()["paths"][
        "/authorization-probe"
    ]["get"]

    assert operation["security"] == [{"BearerAuth": []}]
    assert set(operation["responses"]["403"]["content"]) == {"application/problem+json"}
    example = operation["responses"]["403"]["content"]["application/problem+json"]["example"]
    assert example == {
        "type": "/problems/authorization-denied",
        "title": "권한 없음",
        "status": 403,
        "detail": "현재 역할로는 이 작업을 수행할 수 없습니다.",
        "code": "AUTHORIZATION_DENIED",
    }
    assert "401" in operation["responses"]
