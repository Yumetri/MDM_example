from datetime import UTC, datetime, timedelta
from ipaddress import ip_address
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from httpx import ASGITransport, AsyncClient

from mdm.api.errors import invalid_access_token_handler, validation_error_handler
from mdm.application.auth import HumanPrincipal, InvalidAccessToken
from mdm.application.password_lifecycle import PasswordChangeGrant
from mdm.application.sessions import InvalidSession
from mdm.auth_protection import build_auth_protection
from mdm.domain.auth import UserRole
from mdm.domain.credentials import CsrfToken, RefreshToken, generate_opaque_token
from mdm.infrastructure.settings import Settings

pytestmark = pytest.mark.api
ORIGIN = "https://app.example.net"
RESET = "/api/v1/auth/password-resets"
CHANGE = "/api/v1/auth/me/password"
PASSWORD = "valid new password phrase"
MESSAGE = "비밀번호 재설정이 가능한 계정이면 안내 메일을 보냈습니다. 메일함을 확인해 주세요."


class VerifiedIp:
    def resolve(self, *, peer_host, headers):
        return ip_address("192.0.2.10")


def password_app():
    from mdm.api.password_lifecycle import build_password_router

    protection = build_auth_protection(
        Settings(_env_file=None, auth_allowed_origins=(ORIGIN,), auth_ip_hmac_secret="A" * 43),
        client_ips=VerifiedIp(),
        event_sink=Mock(),
    )
    service = Mock(complete_reset=AsyncMock(), change_password=AsyncMock())
    now = datetime.now(UTC)
    service.change_password.return_value = PasswordChangeGrant(
        generate_opaque_token(RefreshToken), now + timedelta(days=7), now
    )
    requester = Mock(execute=AsyncMock())
    principal = HumanPrincipal(uuid4(), UserRole.USER)
    auth = Mock(return_value=principal)

    def authenticate():
        return auth()

    app = FastAPI()
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.add_exception_handler(InvalidAccessToken, invalid_access_token_handler)
    app.include_router(
        build_password_router(
            reset_router_for=protection.router,
            change_router_for=protection.router,
            cookies=protection.cookies,
            use_cases=lambda: service,
            request_use_case=lambda: requester,
            principal_dependency=authenticate,
        )
    )
    csrf = generate_opaque_token(CsrfToken).reveal()
    refresh = generate_opaque_token(RefreshToken).reveal()
    headers = {
        "Origin": ORIGIN,
        "X-CSRF-Token": csrf,
        "Cookie": f"mdm_csrf={csrf}; mdm_refresh={refresh}",
    }
    return app, service, requester, auth, headers


async def test_reset_request_returns_identical_approved_message_without_browser_credentials():
    app, _, requester, auth, _ = password_app()
    async with AsyncClient(transport=ASGITransport(app), base_url=ORIGIN) as client:
        response = await client.post(RESET, json={"email": "Person@EXAMPLE.NET"})
    assert response.status_code == 202 and response.json() == {"message": MESSAGE}
    assert response.headers["cache-control"] == "no-store"
    assert requester.execute.call_args.args[0].value == "person@example.net"
    auth.assert_not_called()


async def test_reset_completion_is_204_without_auto_login_and_deletes_cookies():
    app, service, _, auth, _ = password_app()
    async with AsyncClient(transport=ASGITransport(app), base_url=ORIGIN) as client:
        response = await client.post(
            RESET + "/complete", json={"token": "A" * 43, "new_password": PASSWORD}
        )
    assert response.status_code == 204 and response.content == b""
    assert len(response.headers.get_list("set-cookie")) == 2
    assert all("Max-Age=0" in h for h in response.headers.get_list("set-cookie"))
    service.complete_reset.assert_awaited_once()
    auth.assert_not_called()


@pytest.mark.parametrize("token", ["", "short", "A" * 42 + "B", "A" * 43 + "="])
async def test_malformed_reset_token_is_400_and_does_not_consume_or_expose_it(token):
    app, service, _, _, _ = password_app()
    async with AsyncClient(transport=ASGITransport(app), base_url=ORIGIN) as client:
        response = await client.post(
            RESET + "/complete", json={"token": token, "new_password": PASSWORD}
        )
    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_PASSWORD_RESET_TOKEN"
    assert "set-cookie" not in response.headers
    service.complete_reset.assert_not_called()


@pytest.mark.parametrize("kind", ["origin", "csrf", "rate_limit", "access"])
async def test_change_protection_precedes_credential_use_case_and_preserves_cookies(kind):
    app, service, _, auth, headers = password_app()
    async with AsyncClient(transport=ASGITransport(app), base_url=ORIGIN) as client:
        if kind == "rate_limit":
            for _ in range(30):
                await client.post(RESET, json={"email": "person@example.net"})
        elif kind == "origin":
            headers.pop("Origin")
        elif kind == "csrf":
            headers.pop("X-CSRF-Token")
        else:
            auth.side_effect = InvalidAccessToken
        response = await client.put(
            CHANGE, headers=headers, json={"current_password": PASSWORD, "new_password": PASSWORD}
        )
    assert (
        response.status_code == {"origin": 403, "csrf": 403, "rate_limit": 429, "access": 401}[kind]
    )
    assert "set-cookie" not in response.headers
    service.change_password.assert_not_called()
    if kind != "access":
        auth.assert_not_called()


async def test_invalid_change_session_clears_only_response_cookies():
    app, service, _, _, headers = password_app()
    service.change_password.side_effect = InvalidSession
    async with AsyncClient(transport=ASGITransport(app), base_url=ORIGIN) as client:
        response = await client.put(
            CHANGE, headers=headers, json={"current_password": PASSWORD, "new_password": PASSWORD}
        )
    assert response.status_code == 401 and response.json()["code"] == "INVALID_SESSION"
    assert all("Max-Age=0" in h for h in response.headers.get_list("set-cookie"))
    assert len(response.headers.get_list("set-cookie")) == 2


async def test_change_success_has_only_rotated_cookies_without_access_token_body():
    app, service, _, _, headers = password_app()
    async with AsyncClient(transport=ASGITransport(app), base_url=ORIGIN) as client:
        response = await client.put(
            CHANGE, headers=headers, json={"current_password": PASSWORD, "new_password": PASSWORD}
        )
    assert response.status_code == 204 and not response.content
    assert len(response.headers.get_list("set-cookie")) == 2
    assert all("Max-Age=0" not in h for h in response.headers.get_list("set-cookie"))
    service.change_password.assert_awaited_once()


async def test_disabled_password_apis_reject_before_body_parsing():
    from mdm.main import create_app

    app = create_app()
    async with AsyncClient(transport=ASGITransport(app), base_url=ORIGIN) as client:
        for method, path in [("POST", RESET), ("POST", RESET + "/complete"), ("PUT", CHANGE)]:
            response = await client.request(method, path, content=b"not-json")
            assert response.status_code == 503
            assert response.json()["code"] == "SERVICE_UNAVAILABLE"
            assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    "path,code", [(RESET + "/complete", "VALIDATION_ERROR"), (CHANGE, "PASSWORD_POLICY_VIOLATION")]
)
async def test_new_password_policy_failure_preserves_cookies_and_credentials(path, code):
    app, service, _, _, headers = password_app()
    body = (
        {"token": "A" * 43, "new_password": "short"}
        if path != CHANGE
        else {"current_password": PASSWORD, "new_password": "short"}
    )
    async with AsyncClient(transport=ASGITransport(app), base_url=ORIGIN) as client:
        response = await client.request(
            "PUT" if path == CHANGE else "POST", path, headers=headers, json=body
        )
    assert response.status_code == 422 and response.json()["code"] == code
    assert "set-cookie" not in response.headers and "short" not in response.text
    service.complete_reset.assert_not_called()
    service.change_password.assert_not_called()


@pytest.mark.parametrize(
    "field,value", [("token", 123), ("new_password", 123), ("actor_role", "SUPER_ADMIN")]
)
async def test_invalid_fields_and_forged_authority_are_rejected_without_secret_echo(field, value):
    app, service, _, _, _ = password_app()
    async with AsyncClient(transport=ASGITransport(app), base_url=ORIGIN) as client:
        response = await client.post(
            RESET + "/complete", json={"token": "A" * 43, "new_password": PASSWORD, field: value}
        )
    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"
    assert PASSWORD not in response.text and "A" * 43 not in response.text
    service.complete_reset.assert_not_called()


@pytest.mark.parametrize(
    "kind,status,code",
    [
        ("reset", 400, "INVALID_PASSWORD_RESET_TOKEN"),
        ("current", 400, "INVALID_CURRENT_PASSWORD"),
        ("hash", 503, "AUTH_PASSWORD_HASH_UNAVAILABLE"),
        ("database", 503, "SERVICE_UNAVAILABLE"),
        ("collision", 503, "SERVICE_UNAVAILABLE"),
    ],
)
async def test_password_errors_are_sanitized_and_preserve_cookies(kind, status, code):
    from mdm.application.auth import PasswordHashUnavailable
    from mdm.application.password_lifecycle import (
        InvalidCurrentPassword,
        InvalidPasswordResetToken,
        PasswordLifecycleUnavailable,
    )
    from mdm.application.tokens import OpaqueTokenCollision

    app, service, _, _, headers = password_app()
    errors = {
        "reset": InvalidPasswordResetToken,
        "current": InvalidCurrentPassword,
        "hash": PasswordHashUnavailable,
        "database": PasswordLifecycleUnavailable,
        "collision": OpaqueTokenCollision,
    }
    method = service.complete_reset if kind == "reset" else service.change_password
    method.side_effect = errors[kind]("private diagnostic and secret")
    body = (
        {"token": "A" * 43, "new_password": PASSWORD}
        if kind == "reset"
        else {"current_password": PASSWORD, "new_password": PASSWORD}
    )
    async with AsyncClient(transport=ASGITransport(app), base_url=ORIGIN) as client:
        response = await client.request(
            "POST" if kind == "reset" else "PUT",
            RESET + "/complete" if kind == "reset" else CHANGE,
            json=body,
            headers=headers,
        )
    assert response.status_code == status and response.json()["code"] == code
    assert "set-cookie" not in response.headers
    assert "private diagnostic" not in response.text
    assert response.headers["cache-control"] == "no-store"


def test_password_openapi_documents_authentication_and_error_boundaries():
    from mdm.main import create_app

    schema = create_app().openapi()
    request = schema["paths"][RESET]["post"]
    complete = schema["paths"][RESET + "/complete"]["post"]
    change = schema["paths"][CHANGE]["put"]
    assert "security" not in request and "security" not in complete
    assert change["security"]
    assert request["responses"]["202"]["content"]["application/json"]["example"] == {
        "message": MESSAGE
    }
    assert {"400", "422", "429", "503"} <= complete["responses"].keys()
    assert {"400", "401", "403", "422", "429", "503"} <= change["responses"].keys()
    assert (
        "content" not in complete["responses"]["204"]
        and "content" not in change["responses"]["204"]
    )
    assert all(p["required"] for p in change["parameters"])
    assert schema["components"]["schemas"]["PasswordChangeRequest"]["additionalProperties"] is False
