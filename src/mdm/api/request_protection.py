"""Reusable routing boundary that protects authentication before parsing request bodies."""

from collections.abc import Callable, Coroutine
from typing import Any

from fastapi import APIRouter, Request, Response
from fastapi.routing import APIRoute
from starlette.types import Message, Receive, Scope, Send

from mdm.api.errors import problem_response
from mdm.api.schemas import ProblemDetails
from mdm.application.request_protection import (
    AuthAction,
    AuthRequestProtection,
    ClientIpResolver,
    CsrfValidationFailed,
    OriginNotAllowed,
    RateLimitExceeded,
    policy_for,
)

_PROBLEMS = {
    "ORIGIN_NOT_ALLOWED": ("허용되지 않은 Origin", "허용된 Origin 하나가 필요합니다."),
    "CSRF_VALIDATION_FAILED": (
        "CSRF 검증 실패",
        "유효한 CSRF cookie와 X-CSRF-Token header가 일치해야 합니다.",
    ),
    "RATE_LIMIT_EXCEEDED": (
        "요청 횟수 제한",
        "인증 요청 횟수를 초과했습니다. Retry-After 이후 다시 시도해 주세요.",
    ),
}


def _problem(code: str, status: int) -> ProblemDetails:
    title, detail = _PROBLEMS[code]
    return ProblemDetails(
        type=f"/problems/{code.lower().replace('_', '-')}",
        title=title,
        status=status,
        detail=detail,
        code=code,
    )


def protection_responses(action: AuthAction) -> dict[int | str, dict[str, Any]]:
    """Document only the protection failures required by this action's policy."""
    policy = policy_for(action)
    responses: dict[int | str, dict[str, Any]] = {}
    codes = []
    if policy.origin:
        codes.append("ORIGIN_NOT_ALLOWED")
    if policy.csrf:
        codes.append("CSRF_VALIDATION_FAILED")
    if codes:
        responses[403] = {
            "model": ProblemDetails,
            "description": (
                "Origin 또는 CSRF 검증에 실패했습니다. 기존 cookie는 유지됩니다."
                if policy.csrf
                else "Origin 검증에 실패했습니다. 기존 cookie는 유지됩니다."
            ),
            "content": {
                "application/problem+json": {
                    "schema": {"$ref": "#/components/schemas/ProblemDetails"},
                    "examples": {
                        code: {"value": _problem(code, 403).model_dump(exclude_none=True)}
                        for code in codes
                    },
                }
            },
        }
    if policy.rate_limit:
        responses[429] = {
            "model": ProblemDetails,
            "description": "인증 요청의 공용 횟수 제한을 초과했습니다. 기존 cookie는 유지됩니다.",
            "headers": {
                "Retry-After": {
                    "description": "재시도 전 기다려야 하는 초입니다.",
                    "schema": {"type": "integer", "minimum": 1, "maximum": 60},
                    "example": 1,
                }
            },
            "content": {
                "application/problem+json": {
                    "schema": {"$ref": "#/components/schemas/ProblemDetails"},
                    "example": _problem("RATE_LIMIT_EXCEEDED", 429).model_dump(exclude_none=True),
                }
            },
        }
    return responses


def _csrf_cookie_values(request: Request) -> tuple[str, ...]:
    # Preserve duplicate occurrences instead of letting a dict silently choose one credential.
    values = []
    for header in request.headers.getlist("cookie"):
        for part in header.split(";"):
            name, separator, value = part.strip().partition("=")
            if name == "mdm_csrf":
                values.append(value if separator else "")
    return tuple(values)


def protected_auth_router(
    *,
    action: AuthAction,
    protection: AuthRequestProtection,
    client_ips: ClientIpResolver,
) -> APIRouter:
    """Use once per action, sharing the same protection object across all credential routes."""
    policy_for(action)

    class ProtectedAuthRoute(APIRoute):
        def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
            original = super().get_route_handler()

            async def protected(request: Request) -> Response:
                client_ip = client_ips.resolve(
                    peer_host=request.client.host if request.client else None,
                    headers=tuple(request.scope["headers"]),
                )
                try:
                    await protection.check(
                        action,
                        origins=tuple(request.headers.getlist("origin")),
                        csrf_cookies=_csrf_cookie_values(request),
                        csrf_headers=tuple(request.headers.getlist("x-csrf-token")),
                        client_ip=client_ip,
                    )
                except (OriginNotAllowed, CsrfValidationFailed, RateLimitExceeded) as exc:
                    code = "ORIGIN_NOT_ALLOWED"
                    status = 403
                    headers: dict[str, str] = {}
                    if isinstance(exc, CsrfValidationFailed):
                        code = "CSRF_VALIDATION_FAILED"
                    elif isinstance(exc, RateLimitExceeded):
                        code, status = "RATE_LIMIT_EXCEEDED", 429
                        headers["Retry-After"] = str(exc.retry_after)
                    problem = _problem(code, status).model_copy(
                        update={"instance": request.url.path}
                    )
                    return problem_response(problem, headers=headers)
                return await original(request)

            return protected

        async def handle(self, scope: Scope, receive: Receive, send: Send) -> None:
            # The outer 500 handler can run after the route's send wrapper has unwound.
            scope["mdm.auth_no_store"] = True

            async def no_store_send(message: Message) -> None:
                if message["type"] == "http.response.start":
                    headers = [
                        (key, value)
                        for key, value in message.get("headers", [])
                        if key.lower() != b"cache-control"
                    ]
                    message = {**message, "headers": [*headers, (b"cache-control", b"no-store")]}
                await send(message)

            await super().handle(scope, receive, no_store_send)

    return APIRouter(route_class=ProtectedAuthRoute, responses=protection_responses(action))
