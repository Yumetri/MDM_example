"""Browser session endpoints using the shared protection and application ports."""

from collections.abc import Callable, Coroutine
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from mdm.api.auth import INVALID_ACCESS_TOKEN_RESPONSE
from mdm.api.errors import problem_response
from mdm.api.request_protection import protection_responses
from mdm.api.schemas import ProblemDetails
from mdm.api.session_cookies import SessionCookies
from mdm.api.session_errors import invalid_session_response
from mdm.application.auth import HumanPrincipal, PasswordHashUnavailable
from mdm.application.request_protection import AuthAction
from mdm.application.sessions import (
    GetCurrentProfile,
    InvalidCredentials,
    InvalidSession,
    RefreshConflict,
    SessionGrant,
    SessionUnavailable,
    SessionUseCases,
)
from mdm.application.users import UserPersistenceError
from mdm.domain.auth import EmailAddress, PlainPassword, UserRole
from mdm.domain.credentials import InvalidOpaqueToken, RefreshToken


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    email: str = Field(
        strict=True, description="로그인할 이메일 주소입니다.", examples=["person@example.net"]
    )
    password: SecretStr = Field(
        examples=["example password phrase"],
        description="NFC 정규화 후 8~128자인 비밀번호입니다. 공백은 유지됩니다.",
    )

    @field_validator("email")
    @classmethod
    def normalized_email(cls, value: str) -> str:
        return EmailAddress(value).value

    @field_validator("password")
    @classmethod
    def normalized_password(cls, value: SecretStr) -> SecretStr:
        return SecretStr(PlainPassword(value.get_secret_value()).reveal())


class AccessTokenResponse(BaseModel):
    access_token: str = Field(description="메모리에만 보관할 RS256 액세스 JWT입니다.")
    token_type: Literal["Bearer"] = Field(
        default="Bearer", description="액세스 토큰 전달 방식입니다."
    )
    expires_in: Literal[900] = Field(
        default=900, description="액세스 토큰의 명목 유효 기간(초)입니다."
    )


class CurrentProfileResponse(BaseModel):
    id: UUID = Field(description="현재 사용자의 고유 식별자입니다.")
    email: str = Field(description="현재 데이터베이스에 저장된 이메일입니다.")
    name: str = Field(description="현재 데이터베이스에 저장된 표시 이름입니다.")
    effective_role: UserRole = Field(
        description=(
            "이 요청의 JWT에 담긴 역할입니다. DB 역할 변경은 다음 로그인·refresh에 반영됩니다."
        )
    )


_PROBLEMS = {
    "INVALID_CREDENTIALS": (401, "로그인 실패", "이메일 또는 비밀번호를 확인해 주세요."),
    "refresh_conflict": (
        409,
        "세션 갱신 충돌",
        "다른 요청이 세션을 갱신했습니다. 1초 뒤 새 cookie로 한 번만 재시도해 주세요.",
    ),
    "AUTH_PASSWORD_HASH_UNAVAILABLE": (
        503,
        "인증 처리 일시 중단",
        "현재 비밀번호를 확인할 수 없습니다. 잠시 후 다시 시도해 주세요.",
    ),
    "SERVICE_UNAVAILABLE": (
        503,
        "서비스를 사용할 수 없음",
        "현재 세션 기능을 사용할 수 없습니다. 잠시 후 다시 시도해 주세요.",
    ),
}


def session_problem(code: str, *, instance: str | None = None) -> ProblemDetails:
    status, title, detail = _PROBLEMS[code]
    return ProblemDetails(
        type=f"/problems/{code.lower().replace('_', '-')}",
        title=title,
        status=status,
        detail=detail,
        code=code,
        instance=instance,
    )


def unavailable_response(instance: str) -> JSONResponse:
    return problem_response(
        session_problem("SERVICE_UNAVAILABLE", instance=instance),
        headers={"Cache-Control": "no-store"},
    )


def unavailable_session_router(action: AuthAction) -> APIRouter:
    class UnavailableRoute(APIRoute):
        def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
            async def unavailable(request: Request) -> Response:
                return unavailable_response(request.url.path)

            return unavailable

    return APIRouter(route_class=UnavailableRoute, responses=protection_responses(action))


def _responses(*codes: str) -> dict[int | str, dict[str, Any]]:
    responses: dict[int | str, dict[str, Any]] = {}
    for code in codes:
        problem = session_problem(code)
        response = responses.setdefault(
            problem.status,
            {
                "model": ProblemDetails,
                "description": "요청을 처리할 수 없습니다. 기존 cookie는 유지됩니다.",
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
        if code == "refresh_conflict":
            response["headers"] = {
                "Retry-After": {
                    "description": "새 cookie로 한 번 재시도하기 전 기다릴 초입니다.",
                    "schema": {"type": "integer", "const": 1},
                    "example": 1,
                }
            }
    return responses


def _refresh_cookie(request: Request) -> RefreshToken | None:
    values = []
    for header in request.headers.getlist("cookie"):
        for part in header.split(";"):
            name, separator, value = part.strip().partition("=")
            if name == "mdm_refresh":
                values.append(value if separator else "")
    if len(values) != 1:
        return None
    try:
        return RefreshToken(values[0])
    except InvalidOpaqueToken:
        return None


def _error(exc: Exception, request: Request, cookies: SessionCookies) -> JSONResponse:
    if isinstance(exc, InvalidSession):
        return invalid_session_response(instance=request.url.path, cookies=cookies)
    code = "SERVICE_UNAVAILABLE"
    if isinstance(exc, InvalidCredentials):
        code = "INVALID_CREDENTIALS"
    elif isinstance(exc, RefreshConflict):
        code = "refresh_conflict"
    elif isinstance(exc, PasswordHashUnavailable):
        code = "AUTH_PASSWORD_HASH_UNAVAILABLE"
    headers = {"Cache-Control": "no-store"}
    if isinstance(exc, RefreshConflict):
        headers["Retry-After"] = "1"
    return problem_response(session_problem(code, instance=request.url.path), headers=headers)


def _grant_body(
    grant: SessionGrant, response: Response, cookies: SessionCookies
) -> AccessTokenResponse:
    cookies.issue(
        response, refresh=grant.refresh_token, expires_at=grant.expires_at, now=grant.issued_at
    )
    return AccessTokenResponse(access_token=grant.access_token.reveal())


def _parameters(action: AuthAction) -> dict[str, Any]:
    parameters: list[dict[str, Any]] = [
        {
            "name": "Origin",
            "in": "header",
            "required": True,
            "description": "허용된 화면의 Origin을 scheme://host[:port] 형식으로 전달합니다.",
            "schema": {"type": "string"},
        }
    ]
    if action in (AuthAction.REFRESH, AuthAction.LOGOUT):
        for name, location, description in (
            (
                "mdm_refresh",
                "cookie",
                (
                    "HttpOnly refresh cookie입니다. Origin·CSRF 검사를 통과해도 이 cookie가 "
                    "없으면 401 INVALID_SESSION을 반환하고 두 cookie를 삭제합니다."
                    if action == AuthAction.REFRESH
                    else "HttpOnly refresh cookie입니다. 로그아웃에서는 생략할 수 있습니다."
                ),
            ),
            ("mdm_csrf", "cookie", "현재 CSRF cookie 값입니다."),
            ("X-CSRF-Token", "header", "mdm_csrf cookie와 같은 값을 전달합니다."),
        ):
            parameters.append(
                {
                    "name": name,
                    "in": location,
                    "required": name != "mdm_refresh",
                    "description": description,
                    "schema": {"type": "string"},
                }
            )
    return {"parameters": parameters}


def build_session_router(
    *,
    router_for: Callable[[AuthAction], APIRouter],
    cookies: SessionCookies,
    use_cases: Callable[[], SessionUseCases | None],
    profile: GetCurrentProfile,
    principal_dependency: Callable[..., HumanPrincipal],
) -> APIRouter:
    result = APIRouter(prefix="/api/v1/auth", tags=["Authentication"])
    login_router = router_for(AuthAction.LOGIN)
    login_responses = _responses(
        "INVALID_CREDENTIALS", "AUTH_PASSWORD_HASH_UNAVAILABLE", "SERVICE_UNAVAILABLE"
    )
    login_responses[422] = {
        "model": ProblemDetails,
        "description": "이메일·비밀번호 또는 JSON 요청 형식이 유효하지 않습니다.",
        "content": {
            "application/problem+json": {
                "schema": {"$ref": "#/components/schemas/ProblemDetails"},
                "example": {
                    "type": "/problems/validation-error",
                    "title": "유효하지 않은 요청",
                    "status": 422,
                    "detail": "하나 이상의 요청 필드가 유효하지 않습니다.",
                    "code": "VALIDATION_ERROR",
                    "violations": [
                        {"field": "body.email", "message": "유효한 이메일이 필요합니다."}
                    ],
                },
            }
        },
    }
    success_headers = {
        "Cache-Control": {
            "description": "응답을 저장하지 않습니다.",
            "schema": {"type": "string", "const": "no-store"},
        },
        "Set-Cookie": {
            "description": "mdm_refresh와 mdm_csrf를 각각 별도 Set-Cookie로 발급합니다. "
            "두 cookie는 세션의 절대 만료 시각에 함께 만료됩니다.",
            "schema": {"type": "string"},
        },
    }
    login_responses[200] = {"description": "로그인에 성공했습니다.", "headers": success_headers}

    @login_router.post(
        "/login",
        operation_id="auth_login",
        response_model=AccessTokenResponse,
        status_code=200,
        summary="비밀번호로 로그인",
        description=(
            "허용된 Origin에서 이메일·비밀번호로 로그인합니다. "
            "미등록·잘못된 비밀번호·비활성 계정은 "
            "같은 401로 거부합니다. 성공하면 기존 세션을 폐기하고 7일 절대 만료 세션을 만듭니다. "
            "mdm_refresh는 HttpOnly, mdm_csrf는 JavaScript로 읽을 수 있는 host-only cookie입니다. "
            "두 cookie는 Path=/, SameSite=Strict이며 운영에서는 Secure입니다. "
            "응답은 Cache-Control: no-store이고 액세스 토큰은 브라우저 메모리에만 보관합니다."
        ),
        responses=login_responses,
        openapi_extra=_parameters(AuthAction.LOGIN),
    )
    async def login(body: LoginRequest, request: Request, response: Response):
        service = use_cases()
        if service is None:
            return unavailable_response(request.url.path)
        try:
            grant = await service.login(
                EmailAddress(body.email), PlainPassword(body.password.get_secret_value())
            )
        except (
            InvalidCredentials,
            PasswordHashUnavailable,
            SessionUnavailable,
            UserPersistenceError,
        ) as exc:
            return _error(exc, request, cookies)
        return _grant_body(grant, response, cookies)

    refresh_router = router_for(AuthAction.REFRESH)
    refresh_responses = _responses("refresh_conflict", "SERVICE_UNAVAILABLE")
    refresh_responses[200] = {"description": "세션을 갱신했습니다.", "headers": success_headers}
    refresh_responses[401] = {
        "model": ProblemDetails,
        "description": "세션이 유효하지 않습니다. refresh·CSRF cookie를 모두 삭제합니다.",
        "content": {
            "application/problem+json": {
                "schema": {"$ref": "#/components/schemas/ProblemDetails"},
                "example": {
                    "type": "/problems/invalid-session",
                    "title": "유효하지 않은 로그인 세션",
                    "status": 401,
                    "code": "INVALID_SESSION",
                    "detail": "유효한 로그인 세션이 없습니다. 다시 로그인해 주세요.",
                },
            }
        },
    }

    @refresh_router.post(
        "/session/refresh",
        operation_id="auth_refresh_session",
        response_model=AccessTokenResponse,
        status_code=200,
        summary="세션 갱신",
        description=(
            "Origin·CSRF 검사 후 refresh cookie로 액세스 토큰과 두 cookie를 회전합니다. "
            "7일 절대 만료 시각은 연장하지 않습니다. 이미 사용한 token의 10초 이내 재제출은 "
            "409 refresh_conflict와 Retry-After: 1을 반환하며 cookie·세션을 유지합니다. "
            "다른 탭의 새 cookie로 한 번만 재시도하세요. 10초 이후 재사용은 해당 세션 전체를 "
            "폐기합니다. 인증 실패 401에서는 두 cookie를 삭제하고 Origin·CSRF 403과 429에서는 "
            "유지합니다. 모든 응답은 Cache-Control: no-store입니다."
        ),
        responses=refresh_responses,
        openapi_extra=_parameters(AuthAction.REFRESH),
    )
    async def refresh(request: Request, response: Response):
        service = use_cases()
        if service is None:
            return unavailable_response(request.url.path)
        token = _refresh_cookie(request)
        if token is None:
            return invalid_session_response(instance=request.url.path, cookies=cookies)
        try:
            grant = await service.refresh(token)
        except (InvalidSession, RefreshConflict, SessionUnavailable) as exc:
            return _error(exc, request, cookies)
        return _grant_body(grant, response, cookies)

    logout_router = router_for(AuthAction.LOGOUT)

    @logout_router.delete(
        "/session",
        operation_id="auth_logout_session",
        response_model=None,
        response_description="로그아웃을 처리하고 refresh·CSRF cookie를 삭제했습니다.",
        status_code=204,
        summary="로그아웃",
        description=(
            "Origin·CSRF 검사 후 현재 유효 token의 세션을 폐기하고 두 cookie를 삭제합니다. "
            "token이 없거나 잘못됐거나 만료·폐기·이미 사용된 경우 DB 세션을 변경하지 않고 "
            "cookie만 삭제하여 같은 204를 반환합니다. Origin·CSRF 403에서는 cookie를 유지합니다. "
            "IP 횟수 제한은 적용하지 않으며 응답은 Cache-Control: no-store입니다."
        ),
        responses=_responses("SERVICE_UNAVAILABLE"),
        openapi_extra=_parameters(AuthAction.LOGOUT),
    )
    async def logout(request: Request):
        service = use_cases()
        if service is None:
            return unavailable_response(request.url.path)
        token = _refresh_cookie(request)
        if token is not None:
            try:
                await service.logout(token)
            except SessionUnavailable as exc:
                return _error(exc, request, cookies)
        response = Response(status_code=204)
        cookies.clear(response)
        return response

    @result.get(
        "/me",
        operation_id="auth_get_current_profile",
        response_model=CurrentProfileResponse,
        response_description="현재 사용자 프로필을 반환합니다.",
        status_code=200,
        summary="현재 사용자 프로필 조회",
        description=(
            "Bearer JWT로 인증합니다. id·email·name은 현재 DB 값이며 effective_role은 이번 요청의 "
            "JWT 역할입니다. DB 역할이 바뀌어도 다음 로그인·refresh 전까지 기존 JWT 역할이 "
            "사용되며 최대 15분 30초까지 유효할 수 있습니다. cookie·Origin·CSRF·IP 횟수 제한은 "
            "사용하지 않습니다. 세션 발급 기능이 비활성화돼도 조회할 수 있습니다."
        ),
        responses={401: INVALID_ACCESS_TOKEN_RESPONSE, **_responses("SERVICE_UNAVAILABLE")},
    )
    async def me(
        request: Request,
        response: Response,
        principal: Annotated[HumanPrincipal, Depends(principal_dependency)],
    ):
        try:
            current = await profile.execute(principal)
        except (SessionUnavailable, UserPersistenceError) as exc:
            return _error(exc, request, cookies)
        response.headers["Cache-Control"] = "no-store"
        return CurrentProfileResponse(
            id=current.id,
            email=current.email,
            name=current.name,
            effective_role=current.effective_role,
        )

    for router in (login_router, refresh_router, logout_router):
        result.include_router(router)
    return result
