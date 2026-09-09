"""FastAPI Bearer authentication boundary for trusted HUMAN principals."""

from collections.abc import Callable
from typing import Annotated, Any

from fastapi import Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from mdm.api.schemas import ProblemDetails
from mdm.application.auth import AuthenticateHumanPrincipal, HumanPrincipal

INVALID_ACCESS_TOKEN_EXAMPLE = {
    "type": "https://api.example.com/problems/invalid-access-token",
    "title": "유효하지 않은 액세스 토큰",
    "status": 401,
    "detail": "유효한 Bearer 액세스 토큰이 필요합니다.",
    "code": "INVALID_ACCESS_TOKEN",
}

INVALID_ACCESS_TOKEN_RESPONSE: dict[str, Any] = {
    "model": ProblemDetails,
    "description": "Bearer 액세스 토큰이 없거나 유효하지 않습니다.",
    "content": {
        "application/problem+json": {
            "schema": {"$ref": "#/components/schemas/ProblemDetails"},
            "example": INVALID_ACCESS_TOKEN_EXAMPLE,
        }
    },
}

_bearer_auth = HTTPBearer(
    auto_error=False,
    bearerFormat="JWT",
    scheme_name="BearerAuth",
    description="RS256 JWT 액세스 토큰을 Bearer 방식으로 전달합니다.",
)


def build_human_principal_dependency(
    authenticate: AuthenticateHumanPrincipal,
) -> Callable[..., HumanPrincipal]:
    """Build a reusable FastAPI dependency without trusting request actor fields."""

    def require_human_principal(
        credentials: Annotated[
            HTTPAuthorizationCredentials | None,
            Security(_bearer_auth),
        ],
    ) -> HumanPrincipal:
        token = None
        if credentials is not None and credentials.scheme.casefold() == "bearer":
            token = credentials.credentials
        return authenticate.execute(token)

    return require_human_principal
