"""Pre-use-case protection shared by the browser credential flows."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from ipaddress import IPv4Address, IPv6Address
from typing import Protocol

from mdm.application.auth import OperationalEvent, OperationalEventName, OperationalEventSink
from mdm.application.origins import canonical_origin
from mdm.application.rate_limits import RateLimitStore
from mdm.domain.credentials import CsrfToken, InvalidOpaqueToken, csrf_tokens_match

type ClientIp = IPv4Address | IPv6Address


class ClientIpResolver(Protocol):
    """Resolve only an address whose deployment trust boundary has been verified."""

    def resolve(
        self, *, peer_host: str | None, headers: tuple[tuple[bytes, bytes], ...]
    ) -> ClientIp | None:
        """Return None when forwarded headers or the peer cannot identify a trusted client."""
        ...


class AuthAction(StrEnum):
    LOGIN = "login"
    REGISTRATION_REQUEST = "registration_request"
    REGISTRATION_COMPLETE = "registration_complete"
    REFRESH = "refresh"
    LOGOUT = "logout"
    PASSWORD_CHANGE = "password_change"
    PASSWORD_RESET_REQUEST = "password_reset_request"
    PASSWORD_RESET_COMPLETE = "password_reset_complete"
    ME = "me"


@dataclass(frozen=True, slots=True)
class ProtectionPolicy:
    origin: bool
    csrf: bool
    rate_limit: bool


def policy_for(action: AuthAction) -> ProtectionPolicy:
    """Return the fixed #44 matrix without allowing request-supplied policy flags."""
    match action:
        case AuthAction.LOGIN | AuthAction.REGISTRATION_COMPLETE:
            return ProtectionPolicy(origin=True, csrf=False, rate_limit=True)
        case AuthAction.REFRESH | AuthAction.PASSWORD_CHANGE:
            return ProtectionPolicy(origin=True, csrf=True, rate_limit=True)
        case AuthAction.LOGOUT:
            return ProtectionPolicy(origin=True, csrf=True, rate_limit=False)
        case AuthAction.ME:
            return ProtectionPolicy(origin=False, csrf=False, rate_limit=False)
        case (
            AuthAction.REGISTRATION_REQUEST
            | AuthAction.PASSWORD_RESET_REQUEST
            | AuthAction.PASSWORD_RESET_COMPLETE
        ):
            return ProtectionPolicy(origin=False, csrf=False, rate_limit=True)
        case _:
            raise ValueError("unknown authentication action")


class OriginNotAllowed(Exception):
    """Origin protection failed before credential or persistence work."""


class CsrfValidationFailed(Exception):
    """The double-submit tokens are absent, ambiguous, malformed or different."""


class RateLimitExceeded(Exception):
    def __init__(self, retry_after: int) -> None:
        self.retry_after = retry_after
        super().__init__("authentication request quota exceeded")


class AuthRequestProtection:
    def __init__(
        self,
        *,
        allowed_origins: frozenset[str],
        rate_limits: RateLimitStore,
        ip_key: Callable[[ClientIp], str],
        event_sink: OperationalEventSink,
        clock: Callable[[], datetime],
    ) -> None:
        self._origins = frozenset(canonical_origin(value) for value in allowed_origins)
        self._rate_limits = rate_limits
        self._ip_key = ip_key
        self._event_sink = event_sink
        self._clock = clock

    async def check(
        self,
        action: AuthAction,
        *,
        origins: tuple[str, ...],
        csrf_cookies: tuple[str, ...],
        csrf_headers: tuple[str, ...],
        client_ip: ClientIp | None,
    ) -> None:
        """Run Origin, CSRF and quota in order, never touching a DB or logging credentials."""
        policy = policy_for(action)
        if policy.origin:
            try:
                if len(origins) != 1 or canonical_origin(origins[0]) not in self._origins:
                    raise ValueError
            except ValueError:
                self._emit("ORIGIN_VALIDATION_FAILED", client_ip)
                raise OriginNotAllowed from None
        if policy.csrf:
            try:
                if len(csrf_cookies) != 1 or len(csrf_headers) != 1:
                    raise InvalidOpaqueToken
                if not csrf_tokens_match(CsrfToken(csrf_cookies[0]), CsrfToken(csrf_headers[0])):
                    raise InvalidOpaqueToken
            except InvalidOpaqueToken:
                self._emit("CSRF_VALIDATION_FAILED", client_ip)
                raise CsrfValidationFailed from None
        if not policy.rate_limit:
            return
        if client_ip is None:
            self._emit("CLIENT_IP_UNRESOLVED", None)
            return
        decision = await self._rate_limits.consume(self._ip_key(client_ip))
        if not decision.allowed:
            self._emit("RATE_LIMIT_EXCEEDED", client_ip)
            raise RateLimitExceeded(decision.retry_after)
        if decision.capacity_exceeded:
            self._emit("RATE_LIMIT_CAPACITY_EXCEEDED", client_ip)

    def _emit(self, name: OperationalEventName, client_ip: ClientIp | None) -> None:
        try:
            self._event_sink.emit(
                OperationalEvent(name=name, occurred_at=self._clock(), client_ip=client_ip)
            )
        except Exception:
            pass
