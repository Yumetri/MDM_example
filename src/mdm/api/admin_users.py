"""Administrator user queries with filter-bound opaque pagination."""

import base64
import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Path, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from mdm.api.auth import INVALID_ACCESS_TOKEN_RESPONSE
from mdm.api.authorization import AUTHORIZATION_DENIED_RESPONSE, build_authorization_guard
from mdm.api.errors import problem_response
from mdm.api.routes import API_V1_PREFIX
from mdm.api.schemas import FieldViolation, ProblemDetails
from mdm.application.admin_users import (
    AdminUserQueries,
    UserCursor,
    UserFilters,
    UserNotFound,
    UserQueryUnavailable,
    UserQueryValidationError,
    UserSummary,
)
from mdm.application.auth import HumanPrincipal
from mdm.application.authorization import AuthorizationAction, AuthorizationPolicy
from mdm.domain.auth import EmailAddress, UserRole, UserStatus

_COLLECTION = f"{API_V1_PREFIX}/admin/users"


class UserParameters(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str | None = Field(
        default=None,
        description=(
            "정규화 후 이메일이 정확히 일치하는 사용자를 조회합니다. 앞뒤 공백을 제거하고 "
            "local-part는 NFC·casefold, 도메인은 IDNA ASCII·소문자로 정규화합니다."
        ),
        examples=["person@example.com"],
    )
    role: UserRole | None = Field(
        default=None,
        description="조회 대상의 현재 역할입니다. 생략하면 역할 필터를 적용하지 않습니다.",
    )
    status: UserStatus | None = Field(
        default=None, description="조회 대상의 현재 상태입니다. 생략하면 두 상태를 모두 조회합니다."
    )
    cursor: str | None = Field(
        default=None,
        min_length=1,
        max_length=512,
        description="직전 next_cursor입니다. 검색 조건을 바꾸면 생략하고 첫 페이지부터 조회합니다.",
    )
    limit: int = Field(
        default=50,
        ge=1,
        le=100,
        description="한 페이지의 최대 항목 수입니다. 기본 50, 최대 100입니다.",
    )

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str | None) -> str | None:
        return None if value is None else EmailAddress(value).value

    def filters(self) -> UserFilters:
        return UserFilters(
            email=None if self.email is None else EmailAddress(self.email),
            role=self.role,
            status=self.status,
        )


class AdminUserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID = Field(description="사용자의 고유 식별자입니다.")
    email: str = Field(description="데이터베이스에 저장된 정규화 이메일입니다.")
    name: str = Field(description="사용자의 현재 표시 이름입니다.")
    role: UserRole = Field(description="사용자의 현재 데이터베이스 역할입니다.")
    status: UserStatus = Field(description="사용자의 현재 상태입니다.")
    created_at: datetime = Field(description="사용자가 생성된 시각입니다. 시간대를 포함합니다.")
    updated_at: datetime = Field(description="사용자 정보가 마지막으로 수정된 시각입니다.")


class AdminUserListResponse(BaseModel):
    items: list[AdminUserResponse] = Field(
        description="가시 범위와 모든 검색 조건을 만족하는 사용자입니다."
    )
    next_cursor: str | None = Field(
        max_length=512,
        description="같은 검색 조건의 다음 페이지 cursor입니다. 마지막 페이지이면 null입니다.",
    )


def _fingerprint(filters: UserFilters) -> str:
    payload = json.dumps(
        {
            "path": _COLLECTION,
            "email": None if filters.email is None else filters.email.value,
            "role": filters.role,
            "status": filters.status,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _encode_cursor(cursor: UserCursor, filters: UserFilters) -> str:
    payload = json.dumps(
        {
            "v": 1,
            "q": _fingerprint(filters),
            "t": cursor.created_at.astimezone(UTC).isoformat(),
            "i": str(cursor.id),
        },
        separators=(",", ":"),
    )
    return base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")


def _decode_cursor(encoded: str | None, filters: UserFilters) -> UserCursor | None:
    if encoded is None:
        return None
    try:
        data = json.loads(
            base64.b64decode(encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True)
        )
        if not isinstance(data, dict) or set(data) != {"v", "q", "t", "i"}:
            raise ValueError
        if type(data["v"]) is not int or data["v"] != 1 or data["q"] != _fingerprint(filters):
            raise ValueError
        cursor = UserCursor(datetime.fromisoformat(data["t"]), UUID(data["i"]))
        if _encode_cursor(cursor, filters) != encoded:
            raise ValueError
        return cursor
    except (
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
        UnicodeError,
        RecursionError,
        OverflowError,
    ):
        raise UserQueryValidationError(
            "query.cursor",
            "cursor가 유효하지 않거나 검색 조건과 다릅니다. 생략하고 다시 조회해 주세요.",
        ) from None


_PROBLEMS = {
    "USER_NOT_FOUND": (404, "사용자를 찾을 수 없음", "조회할 수 있는 사용자가 없습니다."),
    "SERVICE_UNAVAILABLE": (
        503,
        "서비스를 사용할 수 없음",
        "요청을 일시적으로 처리할 수 없습니다. 잠시 후 다시 시도해 주세요.",
    ),
    "VALIDATION_ERROR": (422, "유효하지 않은 요청", "하나 이상의 요청 필드가 유효하지 않습니다."),
}


def _problem(code: str, instance: str) -> ProblemDetails:
    status, title, detail = _PROBLEMS[code]
    return ProblemDetails(
        type=f"/problems/{code.lower().replace('_', '-')}",
        status=status,
        title=title,
        detail=detail,
        code=code,
        instance=instance,
    )


def admin_user_error_handler(request: Request, exc: Exception) -> JSONResponse:
    if isinstance(exc, UserNotFound):
        problem = _problem("USER_NOT_FOUND", request.url.path)
    elif isinstance(exc, UserQueryValidationError):
        problem = _problem("VALIDATION_ERROR", request.url.path)
        problem.violations = [FieldViolation(field=exc.field, message=exc.message)]
    else:
        assert isinstance(exc, UserQueryUnavailable)
        problem = _problem("SERVICE_UNAVAILABLE", request.url.path)
    return problem_response(problem)


def _responses(*, detail: bool = False) -> dict[int | str, dict[str, Any]]:
    responses: dict[int | str, dict[str, Any]] = {
        401: INVALID_ACCESS_TOKEN_RESPONSE,
        403: AUTHORIZATION_DENIED_RESPONSE,
    }
    for code in (
        "VALIDATION_ERROR",
        "SERVICE_UNAVAILABLE",
        *(("USER_NOT_FOUND",) if detail else ()),
    ):
        problem = _problem(code, f"{_COLLECTION}/{_EXAMPLE['id']}" if detail else _COLLECTION)
        responses[problem.status] = {
            "model": ProblemDetails,
            "description": problem.detail,
            "content": {
                "application/problem+json": {
                    "schema": {"$ref": "#/components/schemas/ProblemDetails"},
                    "example": problem.model_dump(exclude_none=True),
                }
            },
        }
    return responses


_EXAMPLE = {
    "id": "019efc13-8c00-7000-8000-000000000001",
    "email": "person@example.com",
    "name": "홍길동",
    "role": "USER",
    "status": "ACTIVE",
    "created_at": "2026-06-25T00:00:00Z",
    "updated_at": "2026-06-25T00:00:00Z",
}
_VISIBILITY = (
    "Bearer JWT로 인증한 ADMIN·SUPER_ADMIN만 조회할 수 있습니다. SUPER_ADMIN은 모든 사용자, "
    "ADMIN은 현재 역할이 USER인 사용자만 조회합니다. 호출자 권한은 JWT 역할을 사용하고 "
    "반환하는 대상 역할·상태는 데이터베이스의 현재 값입니다."
)


def build_admin_user_router(
    *,
    use_cases: AdminUserQueries,
    principal_dependency: Callable[..., HumanPrincipal],
    authorization: AuthorizationPolicy,
) -> APIRouter:
    router = APIRouter(prefix=_COLLECTION, tags=["AdminUsers"])
    guard = build_authorization_guard(
        principal_dependency, authorization, AuthorizationAction.READ_USERS
    )

    @router.get(
        "",
        operation_id="admin_list_users",
        response_model=AdminUserListResponse,
        status_code=200,
        summary="관리자 사용자 목록 조회",
        description=_VISIBILITY + " 제공한 필터를 모두 만족하는 사용자를 생성 시각 내림차순, "
        "같은 시각이면 UUID 내림차순으로 반환합니다. ADMIN이 role=ADMIN 또는 SUPER_ADMIN을 "
        "지정하면 빈 목록입니다. 같은 필터와 변경 없는 데이터에서는 페이지 간 누락·중복이 "
        "없습니다. 조회 도중 데이터가 바뀌면 전체 확인에는 첫 페이지부터 재조회가 필요합니다. "
        "이메일·역할·상태 조건을 바꾸면서 이전 cursor를 보내면 422입니다. 정규화 결과가 같은 "
        "이메일과 limit 변경은 허용합니다. 전체 건수는 제공하지 않습니다. "
        "목록 query는 email, role, status, cursor, limit만 허용하며 그 외 파라미터는 "
        "422 VALIDATION_ERROR로 거부합니다.",
        responses={
            **_responses(),
            200: {
                "content": {
                    "application/json": {
                        "examples": {
                            "users": {
                                "summary": "마지막 페이지",
                                "value": {"items": [_EXAMPLE], "next_cursor": None},
                            },
                            "empty": {
                                "summary": "검색 결과 없음",
                                "value": {"items": [], "next_cursor": None},
                            },
                        }
                    }
                }
            },
        },
    )
    async def list_users(
        principal: Annotated[HumanPrincipal, Depends(guard)],
        params: Annotated[UserParameters, Query()],
    ) -> AdminUserListResponse:
        filters = params.filters()
        after = _decode_cursor(params.cursor, filters)
        page = await use_cases.list_users(principal, filters, after=after, limit=params.limit)
        cursor = None
        if page.has_more and page.items:
            last = page.items[-1]
            cursor = _encode_cursor(UserCursor(last.created_at, last.id), filters)
        return AdminUserListResponse(
            items=[AdminUserResponse.model_validate(item) for item in page.items],
            next_cursor=cursor,
        )

    @router.get(
        "/{user_id}",
        operation_id="admin_get_user",
        response_model=AdminUserResponse,
        status_code=200,
        summary="관리자 사용자 상세 조회",
        description=_VISIBILITY + " 존재하지 않는 사용자와 조회 범위 밖의 사용자는 동일한 "
        "404 USER_NOT_FOUND로 응답합니다. 보안 감사 기록은 생성하지 않습니다.",
        responses={
            **_responses(detail=True),
            200: {"content": {"application/json": {"example": _EXAMPLE}}},
        },
    )
    async def get_user(
        principal: Annotated[HumanPrincipal, Depends(guard)],
        user_id: Annotated[UUID, Path(description="조회할 사용자의 고유 UUID입니다.")],
    ) -> UserSummary:
        return await use_cases.get_user(principal, user_id)

    return router
