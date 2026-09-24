"""Public email-registration endpoints with pre-body authentication protection."""

from collections.abc import Callable, Coroutine
from typing import Any, Literal

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from mdm.api.errors import problem_response
from mdm.api.request_protection import protection_responses
from mdm.api.schemas import ProblemDetails
from mdm.api.session_cookies import SessionCookies
from mdm.api.sessions import AccessTokenResponse
from mdm.application.auth import PasswordHashUnavailable
from mdm.application.registrations import (
    CompleteRegistration,
    EmailDomainNotAllowed,
    InvalidRegistrationToken,
    RegistrationUnavailable,
    RequestRegistration,
)
from mdm.application.request_protection import AuthAction
from mdm.application.tokens import OpaqueTokenCollision
from mdm.domain.auth import DisplayName, EmailAddress, PlainPassword
from mdm.domain.credentials import InvalidOpaqueToken, RegistrationToken

ACCEPTED_MESSAGE = "가입 가능한 이메일이면 인증 안내를 보냈습니다. 메일함을 확인해 주세요."


class RegistrationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    email: str = Field(
        strict=True,
        description="가입할 이메일 주소입니다. 허용된 도메인이어야 합니다.",
        examples=["person@example.net"],
    )

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        return EmailAddress(value).value


class RegistrationCompleteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    token: SecretStr = Field(
        strict=True,
        description=(
            "인증 이메일 링크에서 얻은 43자 가입 토큰입니다. URL·로그·저장소에 남기지 마세요."
        ),
        examples=["A" * 43],
    )
    name: str = Field(
        strict=True,
        description=(
            "NFC 정규화와 앞뒤 공백 제거 후 1~100자인 표시 이름입니다. "
            "제어문자는 허용하지 않습니다."
        ),
        examples=["홍길동"],
    )
    password: SecretStr = Field(
        strict=True,
        description="NFC 정규화 후 8~128자인 비밀번호입니다. 공백·대소문자를 유지합니다.",
        examples=["example password phrase"],
    )

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        return DisplayName(value).value

    @field_validator("password")
    @classmethod
    def normalize_password(cls, value: SecretStr) -> SecretStr:
        return SecretStr(PlainPassword(value.get_secret_value()).reveal())


class RegistrationAcceptedResponse(BaseModel):
    message: Literal["가입 가능한 이메일이면 인증 안내를 보냈습니다. 메일함을 확인해 주세요."] = (
        Field(
            default=ACCEPTED_MESSAGE,
            description="계정 존재 여부나 실제 이메일 전달 결과를 구분하지 않는 공통 안내입니다.",
            examples=[ACCEPTED_MESSAGE],
        )
    )


_PROBLEMS = {
    "INVALID_REGISTRATION_TOKEN": (
        400,
        "유효하지 않은 가입 인증 링크",
        "유효하지 않거나 만료된 가입 인증 링크입니다. 가입을 다시 신청해 주세요.",
    ),
    "EMAIL_DOMAIN_NOT_ALLOWED": (
        422,
        "가입할 수 없는 이메일 도메인",
        "현재 가입이 허용된 이메일 도메인이 아닙니다.",
    ),
    "VALIDATION_ERROR": (422, "유효하지 않은 요청", "하나 이상의 요청 필드가 유효하지 않습니다."),
    "AUTH_PASSWORD_HASH_UNAVAILABLE": (
        503,
        "인증 처리 일시 중단",
        "현재 비밀번호를 처리할 수 없습니다. 잠시 후 다시 시도해 주세요.",
    ),
    "SERVICE_UNAVAILABLE": (
        503,
        "서비스를 사용할 수 없음",
        "현재 가입 기능을 사용할 수 없습니다. 잠시 후 다시 시도해 주세요.",
    ),
}


def registration_problem(code: str, instance: str | None = None) -> ProblemDetails:
    status, title, detail = _PROBLEMS[code]
    return ProblemDetails(
        type="/problems/" + code.lower().replace("_", "-"),
        title=title,
        status=status,
        detail=detail,
        code=code,
        instance=instance,
    )


def _failure(code: str, path: str) -> JSONResponse:
    return problem_response(registration_problem(code, path), headers={"Cache-Control": "no-store"})


def _responses(*codes: str) -> dict[int | str, dict[str, Any]]:
    responses: dict[int | str, dict[str, Any]] = {}
    for code in codes:
        problem = registration_problem(code)
        response = responses.setdefault(
            problem.status,
            {
                "model": ProblemDetails,
                "description": "요청을 처리할 수 없습니다. 기존 로그인 cookie는 유지됩니다.",
                "content": {
                    "application/problem+json": {
                        "schema": {"$ref": "#/components/schemas/ProblemDetails"},
                        "examples": {},
                    }
                },
            },
        )
        response["content"]["application/problem+json"]["examples"][code] = {
            "value": problem.model_dump(exclude_none=True)
        }
    return responses


def unavailable_registration_router(action: AuthAction) -> APIRouter:
    class UnavailableRoute(APIRoute):
        def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
            async def unavailable(request: Request) -> Response:
                return _failure("SERVICE_UNAVAILABLE", request.url.path)

            return unavailable

    return APIRouter(route_class=UnavailableRoute, responses=protection_responses(action))


def build_registration_router(
    *,
    router_for: Callable[[AuthAction], APIRouter],
    cookies: SessionCookies,
    request_use_case: Callable[[], RequestRegistration | None],
    complete_use_case: Callable[[], CompleteRegistration | None],
) -> APIRouter:
    result = APIRouter(prefix="/api/v1/auth", tags=["Authentication"])
    request_router = router_for(AuthAction.REGISTRATION_REQUEST)
    request_responses = _responses(
        "EMAIL_DOMAIN_NOT_ALLOWED", "VALIDATION_ERROR", "SERVICE_UNAVAILABLE"
    )
    request_responses[202] = {
        "description": (
            "가입 신청을 접수했습니다. 계정 존재 여부·메일 전달 결과는 구분하지 않습니다."
        ),
        "content": {"application/json": {"example": {"message": ACCEPTED_MESSAGE}}},
    }

    @request_router.post(
        "/registrations",
        operation_id="auth_request_registration",
        response_model=RegistrationAcceptedResponse,
        status_code=202,
        summary="이메일 인증 가입 신청",
        description=(
            "허용 도메인의 이메일로 가입 인증을 신청합니다. Origin·CSRF는 요구하지 않으며 "
            "다른 인증 요청과 IP별 60초 30회 제한을 공유합니다. "
            "허용되지 않은 도메인은 계정 존재 여부와 관계없이 422입니다. "
            "허용 도메인의 기존 계정과 이메일 발송 실패도 같은 202 본문을 반환합니다. "
            "인증 링크는 최초 신청부터 24시간 유효하며 재발송해도 만료 시각을 연장하지 않습니다. "
            "응답은 Cache-Control: no-store입니다. 가입 기능이 꺼져 있으면 503을 반환합니다."
        ),
        responses=request_responses,
    )
    async def request_registration(body: RegistrationRequest, request: Request):
        service = request_use_case()
        if service is None:
            return _failure("SERVICE_UNAVAILABLE", request.url.path)
        try:
            await service.execute(EmailAddress(body.email))
        except EmailDomainNotAllowed:
            return _failure("EMAIL_DOMAIN_NOT_ALLOWED", request.url.path)
        except (RegistrationUnavailable, OpaqueTokenCollision):
            return _failure("SERVICE_UNAVAILABLE", request.url.path)
        return RegistrationAcceptedResponse()

    complete_router = router_for(AuthAction.REGISTRATION_COMPLETE)
    responses = _responses(
        "INVALID_REGISTRATION_TOKEN",
        "EMAIL_DOMAIN_NOT_ALLOWED",
        "VALIDATION_ERROR",
        "AUTH_PASSWORD_HASH_UNAVAILABLE",
        "SERVICE_UNAVAILABLE",
    )
    responses[201] = {
        "description": "가입을 완료하고 자동 로그인했습니다.",
        "headers": {
            "Cache-Control": {
                "description": "응답을 저장하지 않습니다.",
                "schema": {"type": "string", "const": "no-store"},
            },
            "Set-Cookie": {
                "description": "mdm_refresh와 mdm_csrf를 각각 별도 Set-Cookie로 발급합니다.",
                "schema": {"type": "string"},
            },
        },
    }

    @complete_router.post(
        "/registrations/complete",
        operation_id="auth_complete_registration",
        response_model=AccessTokenResponse,
        status_code=201,
        summary="이메일 인증 가입 완료",
        description=(
            "허용된 Origin에서 가입 토큰·표시 이름·비밀번호를 제출합니다. "
            "기존 로그인이나 CSRF cookie는 필요하지 않으며 "
            "다른 인증 요청과 IP별 60초 30회 제한을 공유합니다. "
            "현재 허용 도메인을 다시 확인하고 USER 역할의 활성 계정을 만듭니다. "
            "잘못된 형식·미존재·만료·이미 사용된 토큰은 같은 400 INVALID_REGISTRATION_TOKEN입니다. "
            "인증 메일 발급 뒤 같은 이메일 계정이 다른 경로로 생성된 경우에도 "
            "같은 오류를 반환합니다. "
            "한 인증 링크로 가입을 완료하면 같은 신청에서 발급한 다른 링크도 사용할 수 없습니다. "
            "이름·비밀번호 또는 요청 필드의 422 오류는 토큰을 소비하지 않습니다. "
            "성공하면 7일 절대 만료의 mdm_refresh(HttpOnly)·mdm_csrf cookie를 발급합니다. "
            "두 cookie는 host-only, Path=/, SameSite=Strict이며 운영에서는 Secure입니다. "
            "액세스 JWT는 메모리에만 보관하며 응답은 Cache-Control: no-store입니다. "
            "실패하면 기존 로그인 cookie를 유지합니다. 가입 기능이 꺼져 있으면 503을 반환합니다."
        ),
        responses=responses,
        openapi_extra={
            "parameters": [
                {
                    "name": "Origin",
                    "in": "header",
                    "required": True,
                    "description": (
                        "허용된 화면의 Origin을 scheme://host[:port] 형식으로 전달합니다."
                    ),
                    "schema": {"type": "string"},
                }
            ]
        },
    )
    async def complete_registration(
        body: RegistrationCompleteRequest, request: Request, response: Response
    ):
        service = complete_use_case()
        if service is None:
            return _failure("SERVICE_UNAVAILABLE", request.url.path)
        try:
            grant = await service.execute(
                RegistrationToken(body.token.get_secret_value()),
                DisplayName(body.name),
                PlainPassword(body.password.get_secret_value()),
            )
        except (InvalidOpaqueToken, InvalidRegistrationToken):
            return _failure("INVALID_REGISTRATION_TOKEN", request.url.path)
        except EmailDomainNotAllowed:
            return _failure("EMAIL_DOMAIN_NOT_ALLOWED", request.url.path)
        except PasswordHashUnavailable:
            return _failure("AUTH_PASSWORD_HASH_UNAVAILABLE", request.url.path)
        except (RegistrationUnavailable, OpaqueTokenCollision):
            return _failure("SERVICE_UNAVAILABLE", request.url.path)
        cookies.issue(
            response, refresh=grant.refresh_token, expires_at=grant.expires_at, now=grant.issued_at
        )
        return AccessTokenResponse(access_token=grant.access_token.reveal())

    result.include_router(request_router)
    result.include_router(complete_router)
    return result
