"""Public service health endpoints."""

from typing import Annotated

from fastapi import APIRouter, Depends, status

from mdm.api.schemas import HealthResponse, ProblemDetails
from mdm.application.health import ReadinessCheck

READINESS_UNAVAILABLE_EXAMPLE = {
    "type": "https://api.example.com/problems/service-unavailable",
    "title": "서비스를 사용할 수 없음",
    "status": 503,
    "detail": "필수 의존 서비스를 사용할 수 없어 현재 요청을 처리할 수 없습니다.",
    "code": "SERVICE_UNAVAILABLE",
    "instance": "/health/ready",
}


def build_health_router(readiness_check: ReadinessCheck) -> APIRouter:
    """Create health routes wired to the supplied application boundary."""
    router = APIRouter(prefix="/health", tags=["Health"])

    def provide_readiness_check() -> ReadinessCheck:
        return readiness_check

    @router.get(
        "/live",
        operation_id="get_service_liveness",
        response_model=HealthResponse,
        status_code=status.HTTP_200_OK,
        summary="서비스 프로세스 실행 여부 확인",
        description=(
            "서비스 프로세스가 요청을 처리할 수 있으면 성공 응답을 반환합니다. "
            "이 확인은 데이터베이스 상태와 무관합니다."
        ),
        responses={
            status.HTTP_200_OK: {
                "description": "서비스 프로세스가 실행 중입니다.",
                "content": {
                    "application/json": {
                        "example": {
                            "status": "ok",
                            "message": "서비스가 실행 중입니다.",
                        }
                    }
                },
            }
        },
    )
    def get_liveness() -> HealthResponse:
        """Confirm that the service process is running without checking dependencies."""
        return HealthResponse(status="ok", message="서비스가 실행 중입니다.")

    @router.get(
        "/ready",
        operation_id="get_service_readiness",
        response_model=HealthResponse,
        status_code=status.HTTP_200_OK,
        summary="서비스 요청 처리 가능 여부 확인",
        description=(
            "요청 처리에 필요한 의존 서비스의 상태를 확인합니다. 서비스를 트래픽에서 "
            "일시적으로 제외해야 하면 503 응답을 반환합니다."
        ),
        responses={
            status.HTTP_200_OK: {
                "description": "서비스가 요청을 처리할 준비가 되었습니다.",
                "content": {
                    "application/json": {
                        "example": {
                            "status": "ok",
                            "message": "서비스가 요청을 처리할 준비가 되었습니다.",
                        }
                    }
                },
            },
            status.HTTP_503_SERVICE_UNAVAILABLE: {
                "model": ProblemDetails,
                "description": "필수 의존 서비스를 사용할 수 없습니다.",
                "content": {
                    "application/problem+json": {
                        "schema": {"$ref": "#/components/schemas/ProblemDetails"},
                        "example": READINESS_UNAVAILABLE_EXAMPLE,
                    }
                },
            },
        },
    )
    async def get_readiness(
        check: Annotated[ReadinessCheck, Depends(provide_readiness_check)],
    ) -> HealthResponse:
        """Confirm that required dependencies are available before accepting traffic."""
        await check.execute()
        return HealthResponse(status="ok", message="서비스가 요청을 처리할 준비가 되었습니다.")

    return router
