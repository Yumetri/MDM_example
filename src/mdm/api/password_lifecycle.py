"""Password reset and authenticated password change HTTP boundaries."""

from collections.abc import Callable, Coroutine
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from mdm.api.auth import INVALID_ACCESS_TOKEN_EXAMPLE
from mdm.api.errors import problem_response
from mdm.api.request_protection import protection_responses
from mdm.api.schemas import ProblemDetails
from mdm.api.session_cookies import SessionCookies
from mdm.api.session_errors import invalid_session_response
from mdm.api.sessions import _refresh_cookie
from mdm.application.auth import HumanPrincipal, PasswordHashUnavailable
from mdm.application.password_lifecycle import (
    InvalidCurrentPassword,
    InvalidPasswordResetToken,
    PasswordLifecycle,
    PasswordLifecycleUnavailable,
    RequestPasswordReset,
)
from mdm.application.request_protection import AuthAction
from mdm.application.sessions import InvalidSession
from mdm.application.tokens import OpaqueTokenCollision
from mdm.domain.auth import EmailAddress, PlainPassword
from mdm.domain.credentials import InvalidOpaqueToken, PasswordResetToken

ACCEPTED_MESSAGE = (
    "비밀번호 재설정이 가능한 계정이면 안내 메일을 보냈습니다. 메일함을 확인해 주세요."
)


class PasswordResetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    email: str = Field(
        strict=True,
        description="비밀번호를 재설정할 계정의 이메일 주소입니다.",
        examples=["person@example.net"],
    )

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        return EmailAddress(value).value


class PasswordResetAcceptedResponse(BaseModel):
    message: Literal[
        "비밀번호 재설정이 가능한 계정이면 안내 메일을 보냈습니다. 메일함을 확인해 주세요."
    ] = Field(
        default=ACCEPTED_MESSAGE,
        description="계정 존재·상태·실제 메일 전달 결과를 구분하지 않는 공통 안내입니다.",
        examples=[ACCEPTED_MESSAGE],
    )


class PasswordResetCompleteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    token: SecretStr = Field(
        strict=True,
        description=(
            "이메일 링크의 fragment에서 읽은 43자 토큰을 요청 본문으로 전달합니다. "
            "읽은 즉시 주소창에서 제거하고 현재 페이지 메모리에만 보관하세요. "
            "로그·cookie·localStorage·sessionStorage에 기록하지 마세요."
        ),
        examples=["A" * 43],
    )
    new_password: SecretStr = Field(
        strict=True,
        description="NFC 정규화 후 8~128자인 새 비밀번호입니다. 공백·대소문자를 유지합니다.",
        examples=["example new password phrase"],
    )


class PasswordChangeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    current_password: SecretStr = Field(
        strict=True,
        description="현재 계정의 비밀번호입니다. NFC 정규화 후 8~128자이며 공백을 유지합니다.",
        examples=["example current password phrase"],
    )
    new_password: SecretStr = Field(
        strict=True,
        description="NFC 정규화 후 8~128자인 새 비밀번호입니다. 공백·대소문자를 유지합니다.",
        examples=["example new password phrase"],
    )

    @field_validator("current_password")
    @classmethod
    def normalize_current_password(cls, value: SecretStr) -> SecretStr:
        return SecretStr(PlainPassword(value.get_secret_value()).reveal())


_PROBLEMS = {
    "INVALID_PASSWORD_RESET_TOKEN": (
        400,
        "유효하지 않은 비밀번호 재설정 링크",
        "유효하지 않거나 만료된 비밀번호 재설정 링크입니다. 재설정을 다시 신청해 주세요.",
    ),
    "INVALID_CURRENT_PASSWORD": (400, "현재 비밀번호 불일치", "현재 비밀번호를 확인해 주세요."),
    "INVALID_SESSION": (
        401,
        "유효하지 않은 로그인 세션",
        "유효한 로그인 세션이 없습니다. 다시 로그인해 주세요.",
    ),
    "VALIDATION_ERROR": (422, "유효하지 않은 요청", "하나 이상의 요청 필드가 유효하지 않습니다."),
    "PASSWORD_POLICY_VIOLATION": (
        422,
        "비밀번호 정책 위반",
        "새 비밀번호는 NFC 정규화 후 8~128자여야 합니다.",
    ),
    "AUTH_PASSWORD_HASH_UNAVAILABLE": (
        503,
        "인증 처리 일시 중단",
        "현재 비밀번호를 처리할 수 없습니다. 잠시 후 다시 시도해 주세요.",
    ),
    "SERVICE_UNAVAILABLE": (
        503,
        "서비스를 사용할 수 없음",
        "현재 비밀번호 기능을 사용할 수 없습니다. 잠시 후 다시 시도해 주세요.",
    ),
}


def password_problem(code: str, instance: str | None = None) -> ProblemDetails:
    status, title, detail = _PROBLEMS[code]
    return ProblemDetails(
        type="/problems/" + code.lower().replace("_", "-"),
        title=title,
        status=status,
        detail=detail,
        code=code,
        instance=instance,
    )


def _failure(code: str, instance: str) -> JSONResponse:
    return problem_response(password_problem(code, instance), headers={"Cache-Control": "no-store"})


def _responses(*codes: str) -> dict[int | str, dict[str, Any]]:
    responses: dict[int | str, dict[str, Any]] = {}
    for code in codes:
        problem = password_problem(code)
        response = responses.setdefault(
            problem.status,
            {
                "model": ProblemDetails,
                "description": "요청을 처리하지 못했습니다. 기존 cookie는 유지됩니다.",
                "content": {
                    "application/problem+json": {
                        "schema": {"$ref": "#/components/schemas/ProblemDetails"},
                        "examples": {},
                    }
                },
            },
        )
        response["content"]["application/problem+json"]["examples"][code] = {
            "value": problem.model_dump(exclude_none=True),
        }
    return responses


def unavailable_password_router(action: AuthAction) -> APIRouter:
    class UnavailableRoute(APIRoute):
        def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
            async def unavailable(request: Request) -> Response:
                return _failure("SERVICE_UNAVAILABLE", request.url.path)

            return unavailable

    return APIRouter(route_class=UnavailableRoute, responses=protection_responses(action))


def build_password_router(
    *,
    reset_router_for: Callable[[AuthAction], APIRouter],
    change_router_for: Callable[[AuthAction], APIRouter],
    cookies: SessionCookies,
    use_cases: Callable[[], PasswordLifecycle | None],
    request_use_case: Callable[[], RequestPasswordReset | None],
    principal_dependency: Callable[..., HumanPrincipal],
) -> APIRouter:
    result = APIRouter(prefix="/api/v1/auth", tags=["Authentication"])
    request_router = reset_router_for(AuthAction.PASSWORD_RESET_REQUEST)
    request_responses = _responses("VALIDATION_ERROR", "SERVICE_UNAVAILABLE")
    request_responses[202] = {
        "description": "비밀번호 재설정 신청을 접수했습니다.",
        "content": {"application/json": {"example": {"message": ACCEPTED_MESSAGE}}},
    }

    @request_router.post(
        "/password-resets",
        operation_id="auth_request_password_reset",
        response_model=PasswordResetAcceptedResponse,
        status_code=202,
        summary="비밀번호 재설정 신청",
        description=(
            "이메일로 비밀번호 재설정을 신청합니다. Origin·CSRF·로그인을 요구하지 않습니다. "
            "정상·미등록·비활성 계정과 메일 발송 실패 모두 같은 202 본문을 반환합니다. "
            "미등록·비활성 계정에는 메일을 보내지 않습니다. 재신청해도 최초 신청부터 30분인 "
            "만료 시각은 연장되지 않으며, 이전 링크도 성공 또는 만료 전까지 사용할 수 있습니다. "
            "다른 인증 요청과 IP별 60초 30회 제한을 공유합니다. "
            "모든 응답은 Cache-Control: no-store이며 기능이 꺼져 있으면 503입니다."
        ),
        responses=request_responses,
    )
    async def request_reset(body: PasswordResetRequest, request: Request):
        service = request_use_case()
        if service is None:
            return _failure("SERVICE_UNAVAILABLE", request.url.path)
        try:
            await service.execute(EmailAddress(body.email))
        except (PasswordLifecycleUnavailable, OpaqueTokenCollision):
            return _failure("SERVICE_UNAVAILABLE", request.url.path)
        return PasswordResetAcceptedResponse()

    complete_router = reset_router_for(AuthAction.PASSWORD_RESET_COMPLETE)
    complete_responses = _responses(
        "INVALID_PASSWORD_RESET_TOKEN",
        "VALIDATION_ERROR",
        "AUTH_PASSWORD_HASH_UNAVAILABLE",
        "SERVICE_UNAVAILABLE",
    )
    complete_responses[204] = {
        "description": "비밀번호를 재설정했습니다. 새 비밀번호로 다시 로그인하세요.",
        "headers": {
            "Set-Cookie": {
                "description": "mdm_refresh와 mdm_csrf를 모두 만료시킵니다.",
                "schema": {"type": "string"},
            }
        },
    }

    @complete_router.post(
        "/password-resets/complete",
        operation_id="auth_complete_password_reset",
        response_model=None,
        status_code=204,
        summary="비밀번호 재설정 완료",
        description=(
            "이메일 링크의 토큰과 새 비밀번호를 제출합니다. "
            "로그인·Origin·CSRF는 요구하지 않습니다. "
            "잘못된 형식·미존재·사용·만료·폐기 토큰은 같은 400 INVALID_PASSWORD_RESET_TOKEN입니다. "
            "요청 형식·새 비밀번호 오류는 422 VALIDATION_ERROR이며 토큰을 소비하지 않습니다. "
            "성공하면 기존 로그인 세션과 같은 신청의 모든 재설정 링크를 무효화하고 두 cookie를 "
            "만료시킵니다. 기존 refresh 세션은 즉시 사용할 수 없지만 이미 발급된 액세스 토큰은 "
            "원래 만료 시각과 30초 clock skew까지 유효할 수 있습니다. "
            "자동 로그인하지 않으며 응답 본문과 액세스 토큰은 없습니다. "
            "실패하면 기존 cookie를 유지합니다. 다른 인증 요청과 IP별 60초 30회 제한을 공유합니다. "
            "모든 응답은 Cache-Control: no-store이며 기능이 꺼져 있으면 503입니다."
        ),
        responses=complete_responses,
    )
    async def complete_reset(body: PasswordResetCompleteRequest, request: Request):
        service = use_cases()
        if service is None:
            return _failure("SERVICE_UNAVAILABLE", request.url.path)
        try:
            password = PlainPassword(body.new_password.get_secret_value())
        except ValueError:
            return _failure("VALIDATION_ERROR", request.url.path)
        try:
            await service.complete_reset(
                PasswordResetToken(body.token.get_secret_value()), password
            )
        except (InvalidOpaqueToken, InvalidPasswordResetToken):
            return _failure("INVALID_PASSWORD_RESET_TOKEN", request.url.path)
        except PasswordHashUnavailable:
            return _failure("AUTH_PASSWORD_HASH_UNAVAILABLE", request.url.path)
        except PasswordLifecycleUnavailable:
            return _failure("SERVICE_UNAVAILABLE", request.url.path)
        response = Response(status_code=204)
        cookies.clear(response)
        return response

    change_router = change_router_for(AuthAction.PASSWORD_CHANGE)
    change_responses = _responses(
        "INVALID_CURRENT_PASSWORD",
        "INVALID_SESSION",
        "VALIDATION_ERROR",
        "PASSWORD_POLICY_VIOLATION",
        "AUTH_PASSWORD_HASH_UNAVAILABLE",
        "SERVICE_UNAVAILABLE",
    )
    change_responses[401]["description"] = (
        "액세스 토큰 오류에서는 cookie를 유지하고, 로그인 세션 오류에서는 두 cookie를 만료시킵니다."
    )
    change_responses[401]["content"]["application/problem+json"]["examples"][
        "INVALID_ACCESS_TOKEN"
    ] = {"value": INVALID_ACCESS_TOKEN_EXAMPLE}
    change_responses[401]["headers"] = {
        "WWW-Authenticate": {
            "description": "액세스 토큰이 없거나 유효하지 않을 때 반환하는 Bearer 인증 안내입니다.",
            "schema": {"type": "string", "const": "Bearer"},
        }
    }
    change_responses[204] = {
        "description": "비밀번호를 변경하고 새 세션 cookie를 발급했습니다.",
        "headers": {
            "Set-Cookie": {
                "description": "새 mdm_refresh와 mdm_csrf를 각각 발급합니다.",
                "schema": {"type": "string"},
            }
        },
    }
    parameters = [
        {
            "name": name,
            "in": location,
            "required": True,
            "description": description,
            "schema": {"type": "string"},
        }
        for name, location, description in (
            ("Origin", "header", "허용된 화면의 scheme://host[:port] 값입니다."),
            ("mdm_refresh", "cookie", "현재 유효한 미사용 HttpOnly refresh cookie입니다."),
            ("mdm_csrf", "cookie", "현재 CSRF cookie 값입니다."),
            ("X-CSRF-Token", "header", "mdm_csrf cookie와 같은 값을 전달합니다."),
        )
    ]

    @change_router.put(
        "/me/password",
        operation_id="auth_change_password",
        response_model=None,
        status_code=204,
        summary="로그인 상태 비밀번호 변경",
        description=(
            "Origin·CSRF·공용 IP 제한·Bearer JWT·세션 소유자·현재 비밀번호 순서로 검사합니다. "
            "액세스 토큰과 refresh cookie가 같은 사용자를 가리켜야 합니다. "
            "이전 refresh token은 10초 유예 없이 401 INVALID_SESSION으로 거부하고 두 cookie만 "
            "만료시킵니다. 현재 비밀번호 불일치는 400 INVALID_CURRENT_PASSWORD, 새 비밀번호 "
            "정책 위반은 422 PASSWORD_POLICY_VIOLATION입니다. "
            "요청 필드 오류는 422 VALIDATION_ERROR입니다. "
            "성공하면 기존 세션과 재설정 링크를 폐기하고 7일 절대 만료의 새 세션을 발급합니다. "
            "mdm_refresh는 HttpOnly이고 mdm_csrf는 JavaScript로 읽을 수 있습니다. "
            "두 cookie는 host-only, Path=/, SameSite=Strict이며 운영에서는 Secure입니다. "
            "204에는 본문·새 액세스 토큰이 없습니다. 기존 액세스 토큰은 원래 만료까지 유효합니다. "
            "실패하면 DB 상태를 변경하지 않습니다. "
            "다른 인증 요청과 IP별 60초 30회 제한을 공유합니다. "
            "모든 응답은 Cache-Control: no-store이며 세션 기능이 꺼져 있으면 503입니다."
        ),
        responses=change_responses,
        openapi_extra={"parameters": parameters},
    )
    async def change_password(
        body: PasswordChangeRequest,
        request: Request,
        principal: Annotated[HumanPrincipal, Depends(principal_dependency)],
    ):
        service = use_cases()
        if service is None:
            return _failure("SERVICE_UNAVAILABLE", request.url.path)
        token = _refresh_cookie(request)
        if token is None:
            return invalid_session_response(instance=request.url.path, cookies=cookies)
        try:
            password = PlainPassword(body.new_password.get_secret_value())
        except ValueError:
            return _failure("PASSWORD_POLICY_VIOLATION", request.url.path)
        try:
            grant = await service.change_password(
                principal, token, PlainPassword(body.current_password.get_secret_value()), password
            )
        except InvalidSession:
            return invalid_session_response(instance=request.url.path, cookies=cookies)
        except InvalidCurrentPassword:
            return _failure("INVALID_CURRENT_PASSWORD", request.url.path)
        except PasswordHashUnavailable:
            return _failure("AUTH_PASSWORD_HASH_UNAVAILABLE", request.url.path)
        except (PasswordLifecycleUnavailable, OpaqueTokenCollision):
            return _failure("SERVICE_UNAVAILABLE", request.url.path)
        response = Response(status_code=204)
        cookies.issue(
            response, refresh=grant.refresh_token, expires_at=grant.expires_at, now=grant.issued_at
        )
        return response

    for router in (request_router, complete_router, change_router):
        result.include_router(router)
    return result
