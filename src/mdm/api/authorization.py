"""FastAPI authorization guards for verified HUMAN principals."""

from collections.abc import Callable
from typing import Annotated, Any

from fastapi import Depends

from mdm.api.schemas import ProblemDetails
from mdm.application.auth import HumanPrincipal
from mdm.application.authorization import AuthorizationAction, AuthorizationPolicy

AUTHORIZATION_DENIED_EXAMPLE = {
    "type": "/problems/authorization-denied",
    "title": "권한 없음",
    "status": 403,
    "detail": "현재 역할로는 이 작업을 수행할 수 없습니다.",
    "code": "AUTHORIZATION_DENIED",
}

AUTHORIZATION_DENIED_RESPONSE: dict[str, Any] = {
    "model": ProblemDetails,
    "description": "인증된 사용자의 현재 역할로는 이 작업을 수행할 수 없습니다.",
    "content": {
        "application/problem+json": {
            "schema": {"$ref": "#/components/schemas/ProblemDetails"},
            "example": AUTHORIZATION_DENIED_EXAMPLE,
        }
    },
}


def build_authorization_guard(
    principal_dependency: Callable[..., HumanPrincipal],
    policy: AuthorizationPolicy,
    action: AuthorizationAction,
) -> Callable[..., HumanPrincipal]:
    """Build a route dependency for one server-selected authorization action."""

    def require_authorized_principal(
        principal: Annotated[HumanPrincipal, Depends(principal_dependency)],
    ) -> HumanPrincipal:
        return policy.authorize(principal, action)

    return require_authorized_principal
