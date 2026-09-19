"""HTTP boundary for authorized role and account-status changes."""

from collections.abc import Callable
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Path, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from mdm.api.auth import INVALID_ACCESS_TOKEN_RESPONSE
from mdm.api.authorization import AUTHORIZATION_DENIED_RESPONSE, build_authorization_guard
from mdm.api.errors import problem_response
from mdm.api.routes import API_V1_PREFIX
from mdm.api.schemas import ProblemDetails
from mdm.application.auth import HumanPrincipal
from mdm.application.authorization import AuthorizationAction, AuthorizationPolicy
from mdm.application.user_management import UserManagement
from mdm.domain.auth import UserRole, UserStatus


class UserRoleChangeRequest(BaseModel):
    """사용자에게 부여할 단일 역할입니다."""

    model_config = ConfigDict(extra="forbid")
    role: UserRole = Field(description="대상 사용자에게 부여할 역할입니다.", examples=["ADMIN"])


class UserStatusChangeRequest(BaseModel):
    """사용자의 새로운 활성 상태입니다."""

    model_config = ConfigDict(extra="forbid")
    status: UserStatus = Field(
        description="ACTIVE는 활성화, DISABLED는 비활성화입니다.", examples=["DISABLED"]
    )


_ERRORS = {
    404: ("USER_NOT_FOUND", "사용자를 찾을 수 없음", "조회할 수 있는 사용자가 없습니다."),
    422: ("VALIDATION_ERROR", "유효하지 않은 요청", "하나 이상의 요청 필드가 유효하지 않습니다."),
    503: (
        "SERVICE_UNAVAILABLE",
        "서비스를 사용할 수 없음",
        "요청을 일시적으로 처리할 수 없습니다. 잠시 후 다시 시도해 주세요.",
    ),
}


def _problem(status: int, instance: str) -> ProblemDetails:
    code, title, detail = _ERRORS[status]
    return ProblemDetails(
        type=f"/problems/{code.lower().replace('_', '-')}",
        status=status,
        title=title,
        detail=detail,
        code=code,
        instance=instance,
    )


def user_management_unavailable_handler(request: Request, exc: Exception) -> JSONResponse:
    return problem_response(_problem(503, request.url.path))


def _responses(action: str) -> dict[int | str, dict[str, Any]]:
    responses: dict[int | str, dict[str, Any]] = {
        204: {
            "description": (
                "변경을 완료했거나, 권한이 있는 요청의 값이 현재 값과 같습니다. "
                "응답 본문은 없습니다."
            )
        },
        401: INVALID_ACCESS_TOKEN_RESPONSE,
        403: AUTHORIZATION_DENIED_RESPONSE,
    }
    for status in _ERRORS:
        problem = _problem(
            status, f"{API_V1_PREFIX}/admin/users/019efc13-8c00-7000-8000-000000000001/{action}"
        )
        responses[status] = {
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


_REFLECTION = (
    " 호출자 권한은 검증된 Bearer JWT의 역할로 판단하고 대상은 현재 저장된 값을 사용합니다. "
    "이미 발급된 access JWT는 만료 시각에 허용 오차 30초를 더한 시점까지 기존 역할로 "
    "사용할 수 있으며, 역할·상태 변경은 다음 로그인·세션 갱신부터 반영됩니다. "
    "마지막 활성 SUPER_ADMIN도 변경할 수 있어 활성 SUPER_ADMIN이 0명이 될 수 있습니다."
)


def build_user_management_router(
    *,
    use_cases: UserManagement,
    principal_dependency: Callable[..., HumanPrincipal],
    authorization: AuthorizationPolicy,
) -> APIRouter:
    router = APIRouter(prefix=f"{API_V1_PREFIX}/admin/users", tags=["AdminUsers"])
    role_guard = build_authorization_guard(
        principal_dependency, authorization, AuthorizationAction.MANAGE_USER_ROLES
    )
    status_guard = build_authorization_guard(
        principal_dependency, authorization, AuthorizationAction.MANAGE_USER_STATUS
    )

    @router.put(
        "/{user_id}/role",
        operation_id="admin_change_user_role",
        response_model=None,
        response_class=Response,
        status_code=204,
        summary="사용자 역할 변경",
        description=(
            "ADMIN은 다른 USER의 역할을 USER 또는 ADMIN으로 변경할 수 있습니다. "
            "SUPER_ADMIN은 다른 USER·ADMIN·SUPER_ADMIN의 역할을 "
            "세 역할 중 하나로 변경할 수 있습니다. "
            "자기 역할 변경은 금지합니다. "
            "권한 검사 후 같은 값이면 204를 반환하며 변경 시각과 감사를 남기지 않습니다. "
            "ADMIN이 USER를 ADMIN으로 승격한 뒤 같은 요청을 반복하면 대상이 동급이므로 403입니다. "
            "실제 변경은 역할과 이전·새 역할 감사를 함께 저장합니다. "
            "기존 로그인 세션과 재설정 링크는 폐기하지 않습니다." + _REFLECTION
        ),
        responses=_responses("role"),
    )
    async def change_role(
        body: UserRoleChangeRequest,
        user_id: Annotated[UUID, Path(description="변경할 대상 사용자의 UUID입니다.")],
        principal: Annotated[HumanPrincipal, Depends(role_guard)],
    ) -> Response:
        await use_cases.change_role(principal, user_id, body.role)
        return Response(status_code=204)

    @router.put(
        "/{user_id}/status",
        operation_id="admin_change_user_status",
        response_model=None,
        response_class=Response,
        status_code=204,
        summary="사용자 활성 상태 변경",
        description=(
            "SUPER_ADMIN만 다른 사용자의 상태를 ACTIVE 또는 DISABLED로 변경할 수 있습니다. "
            "자신의 상태 변경은 같은 값 요청도 403입니다. 권한 검사 후 같은 값이면 204를 반환하며 "
            "변경 시각과 감사를 남기지 않습니다. 비활성화는 상태 변경, "
            "활성 로그인 세션과 비밀번호 재설정 링크 폐기, "
            "이전·새 상태 감사 기록을 함께 처리합니다. "
            "비활성화 이후 새 로그인과 세션 갱신은 즉시 거부합니다. "
            "재활성화는 폐기된 인증수단이나 비활성화 중 요청한 재설정 메일을 복원하지 않습니다."
            + _REFLECTION
        ),
        responses=_responses("status"),
    )
    async def change_status(
        body: UserStatusChangeRequest,
        user_id: Annotated[UUID, Path(description="변경할 대상 사용자의 UUID입니다.")],
        principal: Annotated[HumanPrincipal, Depends(status_guard)],
    ) -> Response:
        await use_cases.change_status(principal, user_id, body.status)
        return Response(status_code=204)

    return router
