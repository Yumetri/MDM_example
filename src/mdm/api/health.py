"""Public service health endpoints."""

from typing import Annotated

from fastapi import APIRouter, Depends, status

from mdm.api.schemas import HealthResponse, ProblemDetails
from mdm.application.health import ReadinessCheck

READINESS_UNAVAILABLE_EXAMPLE = {
    "type": "https://api.example.com/problems/service-unavailable",
    "title": "Service unavailable",
    "status": 503,
    "detail": "The service cannot accept traffic because a required dependency is unavailable.",
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
        summary="Check whether the service process is running",
        description=(
            "Returns a successful response while the service process can handle requests. "
            "This check does not require the database."
        ),
        responses={
            status.HTTP_200_OK: {
                "description": "The service process is running.",
                "content": {
                    "application/json": {
                        "example": {
                            "status": "ok",
                            "message": "The service is running.",
                        }
                    }
                },
            }
        },
    )
    def get_liveness() -> HealthResponse:
        """Confirm that the service process is running without checking dependencies."""
        return HealthResponse(status="ok", message="The service is running.")

    @router.get(
        "/ready",
        operation_id="get_service_readiness",
        response_model=HealthResponse,
        status_code=status.HTTP_200_OK,
        summary="Check whether the service can accept traffic",
        description=(
            "Checks the dependencies required to serve requests. Returns 503 when the service "
            "should temporarily be removed from traffic."
        ),
        responses={
            status.HTTP_200_OK: {
                "description": "The service is ready to accept traffic.",
                "content": {
                    "application/json": {
                        "example": {
                            "status": "ok",
                            "message": "The service is ready.",
                        }
                    }
                },
            },
            status.HTTP_503_SERVICE_UNAVAILABLE: {
                "model": ProblemDetails,
                "description": "A required service dependency is unavailable.",
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
        return HealthResponse(status="ok", message="The service is ready.")

    return router
