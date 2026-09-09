from datetime import UTC, datetime
from uuid import UUID

import pytest

from mdm.application.auth import (
    AccessTokenClaims,
    AuthenticateHumanPrincipal,
    InvalidAccessToken,
    OperationalEvent,
)
from mdm.domain.auth import UserRole

USER_ID = UUID("018f3f0e-7b2a-7e8f-9f62-9876543210ab")
JTI = UUID("123e4567-e89b-42d3-a456-426614174000")
NOW = datetime(2033, 5, 18, 3, 33, 20, tzinfo=UTC)


class StubVerifier:
    def __init__(self, *, invalid: bool = False) -> None:
        self.invalid = invalid
        self.calls: list[tuple[str, int]] = []

    def verify(self, token: str, *, now: int) -> AccessTokenClaims:
        self.calls.append((token, now))
        if self.invalid:
            raise InvalidAccessToken
        return AccessTokenClaims(
            user_id=USER_ID,
            role=UserRole.ADMIN,
            issued_at=now - 10,
            expires_at=now + 890,
            jti=JTI,
        )


class RecordingEventSink:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.events: list[OperationalEvent] = []

    def emit(self, event: OperationalEvent) -> None:
        self.events.append(event)
        if self.fail:
            raise OSError("stream unavailable")


@pytest.mark.unit
def test_authenticate_access_token_returns_human_principal_from_verified_claims() -> None:
    verifier = StubVerifier()
    sink = RecordingEventSink()
    authenticate = AuthenticateHumanPrincipal(verifier, sink, clock=lambda: NOW)

    principal = authenticate.execute("signed-token")

    assert principal.user_id == USER_ID
    assert principal.role is UserRole.ADMIN
    assert verifier.calls == [("signed-token", 2_000_000_000)]
    assert sink.events == []


@pytest.mark.unit
@pytest.mark.parametrize("token", [None, ""])
def test_missing_or_empty_access_token_is_rejected_without_calling_verifier(
    token: str | None,
) -> None:
    verifier = StubVerifier()
    sink = RecordingEventSink()
    authenticate = AuthenticateHumanPrincipal(verifier, sink, clock=lambda: NOW)

    with pytest.raises(InvalidAccessToken):
        authenticate.execute(token, request_id="trusted-request-id")

    assert verifier.calls == []
    assert sink.events == [
        OperationalEvent(
            name="INVALID_ACCESS_TOKEN",
            occurred_at=NOW,
            request_id="trusted-request-id",
        )
    ]


@pytest.mark.unit
def test_invalid_access_token_is_rejected_even_when_event_output_fails() -> None:
    authenticate = AuthenticateHumanPrincipal(
        StubVerifier(invalid=True),
        RecordingEventSink(fail=True),
        clock=lambda: NOW,
    )

    with pytest.raises(InvalidAccessToken):
        authenticate.execute("secret-token")
