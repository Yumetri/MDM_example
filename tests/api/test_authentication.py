from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

import pytest
from fastapi import Depends, FastAPI, status
from httpx import ASGITransport, AsyncClient

from mdm.api.auth import (
    INVALID_ACCESS_TOKEN_RESPONSE,
    build_human_principal_dependency,
)
from mdm.api.openapi import configure_openapi
from mdm.application.auth import (
    AccessTokenClaims,
    AuthenticateHumanPrincipal,
    HumanPrincipal,
    InvalidAccessToken,
    OperationalEvent,
)
from mdm.domain.auth import UserRole
from mdm.main import create_app

USER_ID = UUID("018f3f0e-7b2a-7e8f-9f62-9876543210ab")
JTI = UUID("123e4567-e89b-42d3-a456-426614174000")
NOW = datetime(2033, 5, 18, 3, 33, 20, tzinfo=UTC)


class HealthyReadinessCheck:
    async def execute(self) -> None:
        return None


class StubVerifier:
    def __init__(self) -> None:
        self.tokens: list[str] = []

    def verify(self, token: str, *, now: int) -> AccessTokenClaims:
        self.tokens.append(token)
        if token != "valid-token":
            raise InvalidAccessToken
        return AccessTokenClaims(
            user_id=USER_ID,
            role=UserRole.USER,
            issued_at=now - 1,
            expires_at=now + 899,
            jti=JTI,
        )


class RecordingEventSink:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.events: list[OperationalEvent] = []

    def emit(self, event: OperationalEvent) -> None:
        self.events.append(event)
        if self.fail:
            raise OSError("logging unavailable")


def build_protected_application(
    verifier: StubVerifier,
    sink: RecordingEventSink,
) -> FastAPI:
    application = create_app(readiness_check=HealthyReadinessCheck())
    authenticate = AuthenticateHumanPrincipal(verifier, sink, clock=lambda: NOW)
    principal_dependency: Callable[..., HumanPrincipal] = build_human_principal_dependency(
        authenticate
    )

    @application.get(
        "/protected-probe",
        operation_id="get_protected_probe",
        response_model=dict[str, str],
        status_code=status.HTTP_200_OK,
        summary="보호된 경계 확인",
        description="테스트에서 Bearer 인증 경계를 확인합니다.",
        responses={status.HTTP_401_UNAUTHORIZED: INVALID_ACCESS_TOKEN_RESPONSE},
    )
    def protected_probe(
        principal: Annotated[HumanPrincipal, Depends(principal_dependency)],
    ) -> dict[str, str]:
        return {"user_id": str(principal.user_id), "role": principal.role.value}

    application.openapi_schema = None
    configure_openapi(application)
    return application


@asynccontextmanager
async def client_for(
    verifier: StubVerifier,
    sink: RecordingEventSink,
) -> AsyncGenerator[tuple[AsyncClient, FastAPI]]:
    application = build_protected_application(verifier, sink)
    transport = ASGITransport(app=application, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, application


@pytest.mark.api
async def test_valid_bearer_token_returns_only_verified_human_principal() -> None:
    verifier = StubVerifier()
    sink = RecordingEventSink()

    async with client_for(verifier, sink) as (client, _):
        response = await client.get(
            "/protected-probe?actor_id=attacker&actor_role=SUPER_ADMIN&actor_kind=SYSTEM",
            headers={
                "Authorization": "Bearer valid-token",
                "X-Actor-Id": "attacker",
                "X-Actor-Role": "SUPER_ADMIN",
                "X-Actor-Kind": "SYSTEM",
            },
        )

    assert response.status_code == 200
    assert response.json() == {"user_id": str(USER_ID), "role": "USER"}
    assert verifier.tokens == ["valid-token"]
    assert sink.events == []


@pytest.mark.api
@pytest.mark.parametrize(
    "authorization",
    [None, "Basic credentials", "Bearer invalid-secret-token"],
)
async def test_every_invalid_authorization_reason_uses_the_same_public_401_contract(
    authorization: str | None,
) -> None:
    verifier = StubVerifier()
    sink = RecordingEventSink()
    headers = {} if authorization is None else {"Authorization": authorization}

    async with client_for(verifier, sink) as (client, _):
        response = await client.get("/protected-probe", headers=headers)

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json() == {
        "type": "https://api.example.com/problems/invalid-access-token",
        "title": "유효하지 않은 액세스 토큰",
        "status": 401,
        "detail": "유효한 Bearer 액세스 토큰이 필요합니다.",
        "code": "INVALID_ACCESS_TOKEN",
        "instance": "/protected-probe",
    }
    assert "invalid-secret-token" not in response.text
    assert len(sink.events) == 1
    assert sink.events[0].name == "INVALID_ACCESS_TOKEN"


@pytest.mark.api
async def test_operational_event_failure_does_not_replace_invalid_token_response() -> None:
    async with client_for(StubVerifier(), RecordingEventSink(fail=True)) as (client, _):
        response = await client.get(
            "/protected-probe",
            headers={"Authorization": "Bearer invalid-secret-token"},
        )

    assert response.status_code == 401
    assert response.json()["code"] == "INVALID_ACCESS_TOKEN"
    assert "invalid-secret-token" not in response.text


@pytest.mark.api
def test_protected_operation_documents_bearer_auth_and_problem_details() -> None:
    application = build_protected_application(StubVerifier(), RecordingEventSink())
    schema = application.openapi()
    operation = schema["paths"]["/protected-probe"]["get"]

    assert operation["security"] == [{"BearerAuth": []}]
    assert schema["components"]["securitySchemes"]["BearerAuth"] == {
        "type": "http",
        "scheme": "bearer",
        "bearerFormat": "JWT",
        "description": "RS256 JWT 액세스 토큰을 Bearer 방식으로 전달합니다.",
    }
    assert set(operation["responses"]["401"]["content"]) == {"application/problem+json"}
    assert (
        operation["responses"]["401"]["content"]["application/problem+json"]["example"]["code"]
        == "INVALID_ACCESS_TOKEN"
    )
