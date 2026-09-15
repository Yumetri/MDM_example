from datetime import UTC, datetime
from ipaddress import ip_address

import pytest

from mdm.application.auth import OperationalEvent
from mdm.application.rate_limits import RateLimitDecision
from mdm.application.request_protection import (
    AuthAction,
    AuthRequestProtection,
    CsrfValidationFailed,
    OriginNotAllowed,
    RateLimitExceeded,
)
from mdm.domain.credentials import CsrfToken, generate_opaque_token


class RecordingSink:
    def __init__(self, *, fail: bool = False) -> None:
        self.events: list[OperationalEvent] = []
        self.fail = fail

    def emit(self, event: OperationalEvent) -> None:
        self.events.append(event)
        if self.fail:
            raise OSError("sink failed")


class CountingStore:
    def __init__(self, decision: RateLimitDecision | None = None) -> None:
        self.keys: list[str] = []
        self.decision = decision if decision is not None else RateLimitDecision(allowed=True)

    async def consume(self, key: str) -> RateLimitDecision:
        self.keys.append(key)
        return self.decision


def protection(store: CountingStore, sink: RecordingSink) -> AuthRequestProtection:
    return AuthRequestProtection(
        allowed_origins=frozenset({"https://app.example.net:443"}),
        rate_limits=store,
        ip_key=lambda _: "mdm:rate-limit:auth:" + "a" * 64,
        event_sink=sink,
        clock=lambda: datetime(2033, 1, 1, tzinfo=UTC),
    )


@pytest.mark.unit
async def test_origin_and_csrf_rejections_precede_quota_and_emit_one_sanitized_event() -> None:
    store, sink = CountingStore(), RecordingSink(fail=True)
    guard = protection(store, sink)
    with pytest.raises(OriginNotAllowed):
        await guard.check(
            AuthAction.REFRESH,
            origins=(),
            csrf_cookies=(),
            csrf_headers=(),
            client_ip=ip_address("192.0.2.1"),
        )
    assert not store.keys
    assert [event.name for event in sink.events] == ["ORIGIN_VALIDATION_FAILED"]
    assert sink.events[0].client_ip == ip_address("192.0.2.1")
    sink.events.clear()
    with pytest.raises(CsrfValidationFailed):
        await guard.check(
            AuthAction.REFRESH,
            origins=("https://app.example.net",),
            csrf_cookies=("secret",),
            csrf_headers=("secret",),
            client_ip=None,
        )
    assert not store.keys
    assert [event.name for event in sink.events] == ["CSRF_VALIDATION_FAILED"]
    assert "secret" not in repr(sink.events)


@pytest.mark.unit
async def test_old_equal_csrf_pair_passes_but_old_header_with_new_cookie_fails() -> None:
    first = generate_opaque_token(CsrfToken, random_bytes=lambda _: b"a" * 32).reveal()
    second = generate_opaque_token(CsrfToken, random_bytes=lambda _: b"b" * 32).reveal()
    store, sink = CountingStore(), RecordingSink()
    guard = protection(store, sink)
    await guard.check(
        AuthAction.REFRESH,
        origins=("https://app.example.net",),
        csrf_cookies=(first,),
        csrf_headers=(first,),
        client_ip=ip_address("192.0.2.1"),
    )
    with pytest.raises(CsrfValidationFailed):
        await guard.check(
            AuthAction.REFRESH,
            origins=("https://app.example.net",),
            csrf_cookies=(second,),
            csrf_headers=(first,),
            client_ip=ip_address("192.0.2.1"),
        )
    assert len(store.keys) == 1


@pytest.mark.unit
async def test_unresolved_ip_warns_without_consuming_quota() -> None:
    store, sink = CountingStore(), RecordingSink()
    await protection(store, sink).check(
        AuthAction.REGISTRATION_REQUEST,
        origins=(),
        csrf_cookies=(),
        csrf_headers=(),
        client_ip=None,
    )
    assert not store.keys
    assert [event.name for event in sink.events] == ["CLIENT_IP_UNRESOLVED"]


@pytest.mark.unit
async def test_quota_exceeded_and_capacity_fail_open_each_emit_their_own_event_once() -> None:
    sink = RecordingSink()
    store = CountingStore(RateLimitDecision(allowed=False, retry_after=42))
    with pytest.raises(RateLimitExceeded) as error:
        await protection(store, sink).check(
            AuthAction.PASSWORD_RESET_REQUEST,
            origins=(),
            csrf_cookies=(),
            csrf_headers=(),
            client_ip=ip_address("192.0.2.1"),
        )
    assert error.value.retry_after == 42
    assert [event.name for event in sink.events] == ["RATE_LIMIT_EXCEEDED"]
    sink.events.clear()
    store.decision = RateLimitDecision(allowed=True, capacity_exceeded=True)
    await protection(store, sink).check(
        AuthAction.PASSWORD_RESET_COMPLETE,
        origins=(),
        csrf_cookies=(),
        csrf_headers=(),
        client_ip=ip_address("192.0.2.1"),
    )
    assert [event.name for event in sink.events] == ["RATE_LIMIT_CAPACITY_EXCEEDED"]
