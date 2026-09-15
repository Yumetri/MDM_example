"""Composition boundary for the #37/#38/#39 authentication router builders."""

from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi import APIRouter

from mdm.api.request_protection import protected_auth_router
from mdm.api.session_cookies import SessionCookies
from mdm.application.auth import OperationalEventSink
from mdm.application.browser_policy import BrowserProtectionPolicy
from mdm.application.request_protection import AuthAction, AuthRequestProtection, ClientIpResolver
from mdm.infrastructure.operational_events import JsonLineOperationalEventSink
from mdm.infrastructure.rate_limits import InMemoryRateLimitStore
from mdm.infrastructure.request_protection import HmacClientIpKey, UnresolvedClientIpResolver
from mdm.infrastructure.settings import Settings


@dataclass(frozen=True, slots=True)
class AuthProtectionComponents:
    protection: AuthRequestProtection
    client_ips: ClientIpResolver
    cookies: SessionCookies

    def router(self, action: AuthAction) -> APIRouter:
        """Build an action router sharing the process's protection and quota instance."""
        return protected_auth_router(
            action=action, protection=self.protection, client_ips=self.client_ips
        )


def build_auth_protection(
    settings: Settings,
    *,
    client_ips: ClientIpResolver | None = None,
    event_sink: OperationalEventSink | None = None,
) -> AuthProtectionComponents:
    """Call once at application composition, before registering any authentication routes."""
    if settings.auth_ip_hmac_secret is None:
        raise ValueError("IP-HMAC secret is required to configure authentication protection")
    policy = BrowserProtectionPolicy(
        allowed_origins=settings.auth_allowed_origins,
        allow_insecure_local_cookies=settings.auth_allow_insecure_local_cookies,
    )
    return AuthProtectionComponents(
        protection=AuthRequestProtection(
            allowed_origins=frozenset(policy.allowed_origins),
            rate_limits=InMemoryRateLimitStore(capacity=settings.auth_rate_limit_capacity),
            ip_key=HmacClientIpKey(settings.auth_ip_hmac_secret),
            event_sink=event_sink
            if event_sink is not None
            else JsonLineOperationalEventSink(destination="stderr"),
            clock=lambda: datetime.now(UTC),
        ),
        client_ips=client_ips if client_ips is not None else UnresolvedClientIpResolver(),
        cookies=SessionCookies(policy),
    )
