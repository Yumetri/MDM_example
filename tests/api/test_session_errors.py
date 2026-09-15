import json
from http.cookies import SimpleCookie

import pytest

from mdm.api.session_cookies import SessionCookies
from mdm.application.browser_policy import BrowserProtectionPolicy


@pytest.mark.api
def test_invalid_session_response_hides_cause_and_expires_both_cookies() -> None:
    from mdm.api.session_errors import invalid_session_response

    response = invalid_session_response(
        instance="/api/v1/auth/session/refresh",
        cookies=SessionCookies(BrowserProtectionPolicy()),
    )

    assert response.status_code == 401
    assert response.headers["content-type"] == "application/problem+json"
    assert response.headers["cache-control"] == "no-store"
    assert json.loads(bytes(response.body)) == {
        "type": "/problems/invalid-session",
        "title": "유효하지 않은 로그인 세션",
        "status": 401,
        "detail": "유효한 로그인 세션이 없습니다. 다시 로그인해 주세요.",
        "code": "INVALID_SESSION",
        "instance": "/api/v1/auth/session/refresh",
    }
    cookies = SimpleCookie()
    for header in response.headers.getlist("set-cookie"):
        cookies.load(header)
    assert set(cookies) == {"mdm_refresh", "mdm_csrf"}
    for cookie in cookies.values():
        assert cookie["max-age"] == "0"
        assert cookie["path"] == "/"
        assert cookie["samesite"] == "strict"
        assert cookie["secure"]
        assert not cookie["domain"]
    assert cookies["mdm_refresh"]["httponly"]
    assert not cookies["mdm_csrf"]["httponly"]


@pytest.mark.api
async def test_disabled_session_endpoints_return_503_without_body_parsing() -> None:
    from httpx import ASGITransport, AsyncClient

    from mdm.main import create_app

    app = create_app()
    async with AsyncClient(
        transport=ASGITransport(app), base_url="https://app.example.net"
    ) as client:
        for method, path in (
            ("POST", "/api/v1/auth/login"),
            ("POST", "/api/v1/auth/session/refresh"),
            ("DELETE", "/api/v1/auth/session"),
        ):
            response = await client.request(method, path, content=b"not-json")
            assert response.status_code == 503
            assert response.headers["cache-control"] == "no-store"
            assert "set-cookie" not in response.headers
    assert "/api/v1/auth/me" in app.openapi()["paths"]


@pytest.mark.api
@pytest.mark.parametrize(
    "header",
    [
        None,
        "mdm_refresh=bad",
        "mdm_refresh=",
        "mdm_refresh=" + "A" * 43 + "; mdm_refresh=" + "A" * 43,
        "mdm_refresh=" + "A" * 43 + "=",
    ],
)
def test_refresh_cookie_rejects_missing_malformed_and_ambiguous_credentials(header):
    from starlette.requests import Request

    from mdm.api.sessions import _refresh_cookie

    headers = [] if header is None else [(b"cookie", header.encode())]
    request = Request({"type": "http", "headers": headers})
    assert _refresh_cookie(request) is None
