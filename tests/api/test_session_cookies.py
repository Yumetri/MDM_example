from datetime import UTC, datetime, timedelta
from http.cookies import SimpleCookie

import pytest
from fastapi import Response

from mdm.api.session_cookies import SessionCookies
from mdm.application.browser_policy import BrowserProtectionPolicy
from mdm.domain.credentials import CsrfToken, RefreshToken, generate_opaque_token


def cookies(response: Response) -> SimpleCookie:
    result = SimpleCookie()
    for header in response.headers.getlist("set-cookie"):
        result.load(header)
    return result


@pytest.mark.api
def test_cookie_issue_and_rotation_preserve_absolute_expiry_and_rotate_csrf() -> None:
    helper = SessionCookies(BrowserProtectionPolicy())
    now = datetime(2033, 1, 1, tzinfo=UTC)
    expiry = now + timedelta(days=7)
    first, rotated = Response(), Response()
    refresh = generate_opaque_token(RefreshToken)
    helper.issue(first, refresh=refresh, expires_at=expiry, now=now)
    helper.issue(
        rotated,
        refresh=generate_opaque_token(RefreshToken),
        expires_at=expiry,
        now=now + timedelta(minutes=10),
    )
    one, two = cookies(first), cookies(rotated)
    assert one["mdm_refresh"].value == refresh.reveal()
    assert one["mdm_csrf"].value != two["mdm_csrf"].value
    CsrfToken(one["mdm_csrf"].value)
    CsrfToken(two["mdm_csrf"].value)
    for name in ("mdm_refresh", "mdm_csrf"):
        assert one[name]["secure"]
        assert one[name]["samesite"] == "strict"
        assert one[name]["path"] == "/"
        assert not one[name]["domain"]
        assert one[name]["max-age"] == "604800"
        assert two[name]["max-age"] == "604200"
        assert one[name]["expires"] == two[name]["expires"]
    assert one["mdm_refresh"]["httponly"]
    assert not one["mdm_csrf"]["httponly"]
    assert first.headers["cache-control"] == "no-store"


@pytest.mark.api
def test_cookie_clear_expires_both_cookies_with_matching_attributes() -> None:
    helper = SessionCookies(BrowserProtectionPolicy())
    response = Response(status_code=204)
    helper.clear(response)
    result = cookies(response)
    assert set(result) == {"mdm_refresh", "mdm_csrf"}
    for morsel in result.values():
        assert morsel["max-age"] == "0"
        assert morsel["secure"]
        assert morsel["samesite"] == "strict"
        assert morsel["path"] == "/"
        assert not morsel["domain"]
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.api
def test_only_explicit_loopback_http_policy_allows_insecure_cookies() -> None:
    helper = SessionCookies(
        BrowserProtectionPolicy(
            allowed_origins=("http://localhost:8000",),
            allow_insecure_local_cookies=True,
        )
    )
    response = Response()
    now = datetime(2033, 1, 1, tzinfo=UTC)
    helper.issue(
        response,
        refresh=generate_opaque_token(RefreshToken),
        expires_at=now + timedelta(days=7),
        now=now,
    )
    assert not cookies(response)["mdm_refresh"]["secure"]


@pytest.mark.api
def test_naive_expiry_does_not_emit_cookies() -> None:
    response = Response()
    with pytest.raises(ValueError):
        SessionCookies(BrowserProtectionPolicy()).issue(
            response,
            refresh=generate_opaque_token(RefreshToken),
            expires_at=datetime(2033, 1, 1),
            now=datetime(2033, 1, 1, tzinfo=UTC),
        )
    assert "set-cookie" not in response.headers
