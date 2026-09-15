import base64
from ipaddress import ip_address
from typing import Annotated

import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel, Field

from mdm.api.errors import unexpected_error_handler
from mdm.api.openapi import configure_openapi
from mdm.application.auth import OperationalEvent
from mdm.application.request_protection import AuthAction, ClientIp
from mdm.auth_protection import build_auth_protection
from mdm.domain.credentials import CsrfToken, generate_opaque_token
from mdm.infrastructure.settings import Settings

ORIGIN = "https://app.example.net"
TOKEN = generate_opaque_token(CsrfToken, random_bytes=lambda _: b"a" * 32).reveal()
MATRIX = [
    (AuthAction.LOGIN, True, False, True),
    (AuthAction.REGISTRATION_REQUEST, False, False, True),
    (AuthAction.REGISTRATION_COMPLETE, True, False, True),
    (AuthAction.REFRESH, True, True, True),
    (AuthAction.LOGOUT, True, True, False),
    (AuthAction.PASSWORD_CHANGE, True, True, True),
    (AuthAction.PASSWORD_RESET_REQUEST, False, False, True),
    (AuthAction.PASSWORD_RESET_COMPLETE, False, False, True),
    (AuthAction.ME, False, False, False),
]


class Sink:
    def __init__(self) -> None:
        self.events: list[OperationalEvent] = []

    def emit(self, event: OperationalEvent) -> None:
        self.events.append(event)


class VerifiedTestIpResolver:
    def resolve(
        self, *, peer_host: str | None, headers: tuple[tuple[bytes, bytes], ...]
    ) -> ClientIp | None:
        del peer_host, headers
        return ip_address("192.0.2.1")


class Payload(BaseModel):
    value: int = Field(description="테스트 처리에 전달할 정수입니다.")


def build_app(*, trusted_ip: bool = True, fail: bool = False) -> tuple[FastAPI, list[str], Sink]:
    app = FastAPI(title="인증 요청 보호 검증")
    app.add_exception_handler(Exception, unexpected_error_handler)
    calls: list[str] = []
    sink = Sink()
    components = build_auth_protection(
        Settings(
            _env_file=None,
            database_url="postgresql://local",
            auth_allowed_origins=(ORIGIN,),
            auth_ip_hmac_secret=base64.urlsafe_b64encode(b"k" * 32).rstrip(b"=").decode(),
        ),
        client_ips=VerifiedTestIpResolver() if trusted_ip else None,
        event_sink=sink,
    )

    def open_db_session() -> str:
        calls.append("database")
        if fail:
            raise RuntimeError("private failure detail")
        return "session"

    for action in AuthAction:
        router = components.router(action)

        @router.post(
            f"/probe/{action}",
            operation_id=f"probe_{action}",
            response_model=dict[str, str],
            status_code=200,
            tags=["인증 요청 보호"],
            summary="인증 보호 검사",
            description="테스트 전용 보호 경계입니다.",
        )
        def probe(
            payload: Payload, session: Annotated[str, Depends(open_db_session)]
        ) -> dict[str, str]:
            del payload, session
            calls.append("use_case")
            return {"status": "ok"}

        app.include_router(router)
    configure_openapi(app)
    return app, calls, sink


@pytest.mark.api
async def test_unexpected_auth_error_uses_existing_safe_problem_and_no_store() -> None:
    app, _, _ = build_app(fail=True)
    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False), base_url=ORIGIN
    ) as client:
        response = await client.post("/probe/me", json={"value": 1})
    assert response.status_code == 500
    assert response.json()["code"] == "INTERNAL_ERROR"
    assert "private failure detail" not in response.text
    assert response.headers["cache-control"] == "no-store"
    assert "set-cookie" not in response.headers


@pytest.mark.api
def test_protection_openapi_documents_actual_problem_media_type_and_matching_status() -> None:
    app, _, _ = build_app()
    for path in app.openapi()["paths"].values():
        for code, response in path["post"]["responses"].items():
            if code not in ("403", "429"):
                continue
            assert set(response["content"]) == {"application/problem+json"}
            media = response["content"]["application/problem+json"]
            examples = (
                [media["example"]]
                if "example" in media
                else [example["value"] for example in media["examples"].values()]
            )
            assert all(example["status"] == int(code) for example in examples)


@pytest.mark.api
async def test_default_resolver_warns_and_ignores_forged_forwarding_headers() -> None:
    app, _, sink = build_app(trusted_ip=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url=ORIGIN) as client:
        for _ in range(31):
            response = await client.post(
                "/probe/registration_request",
                json={"value": 1},
                headers={"X-Forwarded-For": "198.51.100.1"},
            )
            assert response.status_code == 200
    assert len(sink.events) == 31
    assert all(
        event.name == "CLIENT_IP_UNRESOLVED" and event.client_ip is None for event in sink.events
    )


@pytest.mark.api
@pytest.mark.parametrize(("action", "needs_origin", "needs_csrf", "limited"), MATRIX)
async def test_each_action_enforces_its_policy_before_body_and_database(
    action: AuthAction,
    needs_origin: bool,
    needs_csrf: bool,
    limited: bool,
) -> None:
    del limited
    app, calls, sink = build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url=ORIGIN) as client:
        headers = {"Content-Type": "application/json"}
        response = await client.post(f"/probe/{action}", content="{", headers=headers)
        assert response.status_code == (403 if needs_origin else 422)
        assert response.headers["cache-control"] == "no-store"
        if needs_origin:
            assert response.json()["code"] == "ORIGIN_NOT_ALLOWED"
            assert "set-cookie" not in response.headers
        assert not calls
        headers["Origin"] = ORIGIN
        response = await client.post(f"/probe/{action}", content="{", headers=headers)
        assert response.status_code == (403 if needs_csrf else 422)
        if needs_csrf:
            assert response.json()["code"] == "CSRF_VALIDATION_FAILED"
            assert "set-cookie" not in response.headers
        assert not calls
        headers.update({"Cookie": f"mdm_csrf={TOKEN}", "X-CSRF-Token": TOKEN})
        response = await client.post(f"/probe/{action}", json={"value": 1}, headers=headers)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
    assert calls == ["database", "use_case"]
    assert TOKEN not in repr(sink.events)


@pytest.mark.api
async def test_shared_quota_counts_invalid_json_and_valid_requests_across_routes() -> None:
    app, calls, sink = build_app()
    headers = {"Origin": ORIGIN, "Content-Type": "application/json"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url=ORIGIN) as client:
        for index in range(30):
            action = AuthAction.LOGIN if index % 2 else AuthAction.REGISTRATION_REQUEST
            response = await client.post(f"/probe/{action}", content="{", headers=headers)
            assert response.status_code == 422
        response = await client.post("/probe/password_reset_complete", json={"value": 1})
        assert response.status_code == 429
        assert response.json()["code"] == "RATE_LIMIT_EXCEEDED"
        assert 1 <= int(response.headers["retry-after"]) <= 60
        assert "set-cookie" not in response.headers
        assert response.headers["cache-control"] == "no-store"
        assert not calls
        for action in (AuthAction.ME, AuthAction.LOGOUT):
            response = await client.post(
                f"/probe/{action}",
                json={"value": 1},
                headers={**headers, "Cookie": f"mdm_csrf={TOKEN}", "X-CSRF-Token": TOKEN},
            )
            assert response.status_code == 200
    assert [event.name for event in sink.events] == ["RATE_LIMIT_EXCEEDED"]


@pytest.mark.api
@pytest.mark.parametrize(("action", "needs_origin", "needs_csrf", "limited"), MATRIX)
async def test_every_matrix_action_has_the_documented_quota_membership(
    action: AuthAction,
    needs_origin: bool,
    needs_csrf: bool,
    limited: bool,
) -> None:
    del needs_origin, needs_csrf
    app, _, sink = build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url=ORIGIN) as client:
        for index in range(31):
            response = await client.post(
                f"/probe/{action}",
                json={"value": 1},
                headers={"Origin": ORIGIN, "Cookie": f"mdm_csrf={TOKEN}", "X-CSRF-Token": TOKEN},
            )
            assert response.status_code == (429 if limited and index == 30 else 200)
    assert len(sink.events) == (1 if limited else 0)


@pytest.mark.api
@pytest.mark.parametrize(
    "headers",
    [
        [("Origin", ORIGIN), ("Origin", ORIGIN)],
        [
            ("Origin", ORIGIN),
            ("Cookie", f"mdm_csrf={TOKEN}; mdm_csrf={TOKEN}"),
            ("X-CSRF-Token", TOKEN),
        ],
        [
            ("Origin", ORIGIN),
            ("Cookie", f"mdm_csrf={TOKEN}"),
            ("X-CSRF-Token", TOKEN),
            ("X-CSRF-Token", TOKEN),
        ],
    ],
)
async def test_ambiguous_security_fields_are_rejected(headers: list[tuple[str, str]]) -> None:
    app, calls, sink = build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url=ORIGIN) as client:
        response = await client.post("/probe/refresh", json={"value": 1}, headers=headers)
    assert response.status_code == 403
    assert not calls
    assert len(sink.events) == 1
    assert "set-cookie" not in response.headers
