from datetime import UTC, datetime, timedelta
from ipaddress import ip_address
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from httpx import ASGITransport, AsyncClient

from mdm.api.errors import validation_error_handler
from mdm.application.auth import EncodedAccessToken
from mdm.application.sessions import SessionGrant
from mdm.auth_protection import build_auth_protection
from mdm.domain.credentials import RefreshToken, generate_opaque_token
from mdm.infrastructure.settings import Settings

pytestmark = pytest.mark.api
ORIGIN = "https://app.example.net"
PATH = "/api/v1/auth/registrations"
MESSAGE = "가입 가능한 이메일이면 인증 안내를 보냈습니다. 메일함을 확인해 주세요."
VALID = {"token": "A" * 43, "name": "가입 사용자", "password": "valid password phrase"}


class VerifiedIp:
    def resolve(self, *, peer_host, headers):
        return ip_address("192.0.2.10")


def registration_app():
    from mdm.api.registrations import build_registration_router

    protection = build_auth_protection(
        Settings(_env_file=None, auth_allowed_origins=(ORIGIN,), auth_ip_hmac_secret="A" * 43),
        client_ips=VerifiedIp(),
        event_sink=Mock(),
    )
    request = Mock()
    request.execute = AsyncMock()
    complete = Mock()
    now = datetime.now(UTC)
    complete.execute = AsyncMock(
        return_value=SessionGrant(
            EncodedAccessToken("signed-access"),
            generate_opaque_token(RefreshToken),
            now + timedelta(days=7),
            now,
        )
    )
    app = FastAPI()
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.include_router(
        build_registration_router(
            router_for=protection.router,
            cookies=protection.cookies,
            request_use_case=lambda: request,
            complete_use_case=lambda: complete,
        )
    )
    return app, request, complete


async def test_registration_request_returns_approved_body_without_origin_or_csrf():
    app, request, _ = registration_app()
    async with AsyncClient(transport=ASGITransport(app), base_url=ORIGIN) as client:
        response = await client.post(PATH, json={"email": "Person@EXAMPLE.NET"})
    assert response.status_code == 202
    assert response.json() == {"message": MESSAGE}
    assert response.headers["cache-control"] == "no-store"
    request.execute.assert_awaited_once()
    assert request.execute.call_args.args[0].value == "person@example.net"


async def test_registration_completion_requires_origin_before_body_or_use_case():
    app, _, complete = registration_app()
    async with AsyncClient(transport=ASGITransport(app), base_url=ORIGIN) as client:
        response = await client.post(PATH + "/complete", content=b"not-json")
    assert response.status_code == 403
    assert response.json()["code"] == "ORIGIN_NOT_ALLOWED"
    assert "set-cookie" not in response.headers
    complete.execute.assert_not_called()


@pytest.mark.parametrize("token", ["", "short-secret", "A" * 42 + "B", "A" * 43 + "="])
async def test_malformed_registration_tokens_use_approved_400_without_leaking_input(token):
    app, _, complete = registration_app()
    async with AsyncClient(transport=ASGITransport(app), base_url=ORIGIN) as client:
        response = await client.post(
            PATH + "/complete", json={**VALID, "token": token}, headers={"Origin": ORIGIN}
        )
    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_REGISTRATION_TOKEN"
    assert response.headers["cache-control"] == "no-store"
    assert "set-cookie" not in response.headers
    if token:
        assert token not in response.text
    complete.execute.assert_not_called()


@pytest.mark.parametrize(
    "changes", [{"name": ""}, {"password": "short"}, {"token": 123}, {"actor_role": "SUPER_ADMIN"}]
)
async def test_registration_validation_failure_does_not_consume_token(changes):
    app, _, complete = registration_app()
    async with AsyncClient(transport=ASGITransport(app), base_url=ORIGIN) as client:
        response = await client.post(
            PATH + "/complete", json={**VALID, **changes}, headers={"Origin": ORIGIN}
        )
    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"
    assert VALID["token"] not in response.text
    assert VALID["password"] not in response.text
    complete.execute.assert_not_called()


async def test_registration_success_issues_both_cookies_after_completion():
    app, _, complete = registration_app()
    async with AsyncClient(transport=ASGITransport(app), base_url=ORIGIN) as client:
        response = await client.post(PATH + "/complete", json=VALID, headers={"Origin": ORIGIN})
    assert response.status_code == 201
    assert response.json() == {
        "access_token": "signed-access",
        "token_type": "Bearer",
        "expires_in": 900,
    }
    assert response.headers["cache-control"] == "no-store"
    cookies = response.headers.get_list("set-cookie")
    assert len(cookies) == 2
    assert all(
        "Secure" in cookie and "SameSite=strict" in cookie and "Path=/" in cookie
        for cookie in cookies
    )
    assert "HttpOnly" in cookies[0]
    assert "HttpOnly" not in cookies[1]
    complete.execute.assert_awaited_once()


async def test_registration_actions_share_one_rate_limit():
    app, request, complete = registration_app()
    async with AsyncClient(transport=ASGITransport(app), base_url=ORIGIN) as client:
        for index in range(30):
            response = await client.post(
                PATH if index % 2 else PATH + "/complete",
                json={"email": "person@example.net"} if index % 2 else VALID,
                headers={"Origin": ORIGIN},
            )
            assert response.status_code in (201, 202)
        response = await client.post(PATH, json={"email": "person@example.net"})
    assert response.status_code == 429
    assert response.headers["retry-after"]
    assert request.execute.await_count == complete.execute.await_count == 15


async def test_disabled_registration_rejects_before_body_parsing():
    from mdm.main import create_app

    app = create_app()
    async with AsyncClient(transport=ASGITransport(app), base_url=ORIGIN) as client:
        for path in (PATH, PATH + "/complete"):
            response = await client.post(path, content=b"not-json")
            assert response.status_code == 503
            assert response.json()["code"] == "SERVICE_UNAVAILABLE"
            assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    "kind,status,code",
    [
        ("invalid", 400, "INVALID_REGISTRATION_TOKEN"),
        ("domain", 422, "EMAIL_DOMAIN_NOT_ALLOWED"),
        ("hash", 503, "AUTH_PASSWORD_HASH_UNAVAILABLE"),
        ("database", 503, "SERVICE_UNAVAILABLE"),
        ("collision", 503, "SERVICE_UNAVAILABLE"),
    ],
)
async def test_registration_failure_contract_preserves_cookies_and_hides_diagnostics(
    kind, status, code
):
    from mdm.application.auth import PasswordHashUnavailable
    from mdm.application.registrations import (
        EmailDomainNotAllowed,
        InvalidRegistrationToken,
        RegistrationUnavailable,
    )
    from mdm.application.tokens import OpaqueTokenCollision

    failures = {
        "invalid": InvalidRegistrationToken,
        "domain": EmailDomainNotAllowed,
        "hash": PasswordHashUnavailable,
        "database": RegistrationUnavailable,
        "collision": OpaqueTokenCollision,
    }
    app, _, complete = registration_app()
    complete.execute.side_effect = failures[kind]("private diagnostic and secret")
    async with AsyncClient(transport=ASGITransport(app), base_url=ORIGIN) as client:
        response = await client.post(
            PATH + "/complete",
            json=VALID,
            headers={"Origin": ORIGIN, "Cookie": "mdm_refresh=existing; mdm_csrf=existing"},
        )
    assert response.status_code == status
    assert response.json()["code"] == code
    assert "private diagnostic" not in response.text
    assert "set-cookie" not in response.headers
    assert response.headers["cache-control"] == "no-store"


def test_registration_openapi_documents_approved_responses_and_input_boundaries():
    from mdm.main import create_app

    schema = create_app().openapi()
    request = schema["paths"][PATH]["post"]
    complete = schema["paths"][PATH + "/complete"]["post"]
    assert request["operationId"] == "auth_request_registration"
    assert complete["operationId"] == "auth_complete_registration"
    assert request["responses"]["202"]["content"]["application/json"]["example"] == {
        "message": MESSAGE
    }
    assert {"400", "403", "422", "429", "503"} <= complete["responses"].keys()
    assert "security" not in request and "security" not in complete
    assert complete["parameters"][0]["name"] == "Origin"
    assert complete["parameters"][0]["required"]
    inputs = schema["components"]["schemas"]["RegistrationCompleteRequest"]
    assert set(inputs["required"]) == {"token", "name", "password"}
    assert inputs["additionalProperties"] is False
    assert inputs["properties"]["token"]["writeOnly"] is True
