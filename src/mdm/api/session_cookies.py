"""Commit-success-only session cookie issuance and explicit credential rejection cleanup."""

import math
from datetime import UTC, datetime

from fastapi import Response

from mdm.application.browser_policy import BrowserProtectionPolicy
from mdm.domain.credentials import CsrfToken, RefreshToken, generate_opaque_token


class SessionCookies:
    def __init__(self, policy: BrowserProtectionPolicy) -> None:
        self._secure = policy.secure

    def issue(
        self,
        response: Response,
        *,
        refresh: RefreshToken,
        expires_at: datetime,
        now: datetime,
    ) -> None:
        """Call after commit, using the existing family's absolute expiry on every rotation."""
        if expires_at.utcoffset() is None or now.utcoffset() is None:
            raise ValueError("session cookie timestamps must be timezone-aware")
        max_age = max(0, math.floor((expires_at - now).total_seconds()))
        csrf = generate_opaque_token(CsrfToken)
        for name, value, http_only in (
            ("mdm_refresh", refresh.reveal(), True),
            ("mdm_csrf", csrf.reveal(), False),
        ):
            response.set_cookie(
                name,
                value,
                max_age=max_age,
                expires=expires_at.astimezone(UTC),
                path="/",
                secure=self._secure,
                httponly=http_only,
                samesite="strict",
            )
        response.headers["Cache-Control"] = "no-store"

    def clear(self, response: Response) -> None:
        """Call for logout or credential rejection, never for Origin/CSRF or refresh conflict."""
        for name, http_only in (("mdm_refresh", True), ("mdm_csrf", False)):
            response.delete_cookie(
                name, path="/", secure=self._secure, httponly=http_only, samesite="strict"
            )
        response.headers["Cache-Control"] = "no-store"
