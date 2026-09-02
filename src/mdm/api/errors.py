"""Consistent translation of internal failures to public problem details."""

from typing import Any, cast

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from mdm.api.schemas import FieldViolation, ProblemDetails
from mdm.application.health import ReadinessUnavailable


def problem_response(problem: ProblemDetails) -> JSONResponse:
    return JSONResponse(
        status_code=problem.status,
        content=problem.model_dump(mode="json", exclude_none=True),
        media_type="application/problem+json",
    )


async def readiness_unavailable_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, ReadinessUnavailable)
    return problem_response(
        ProblemDetails(
            type="https://api.example.com/problems/service-unavailable",
            title="Service unavailable",
            status=503,
            detail=(
                "The service cannot accept traffic because a required dependency is unavailable."
            ),
            code="SERVICE_UNAVAILABLE",
            instance=request.url.path,
        )
    )


async def validation_error_handler(request: Request, exc: Exception) -> JSONResponse:
    validation_error = cast(RequestValidationError, exc)
    violations = [
        FieldViolation(
            field=".".join(str(part) for part in error["loc"]),
            message=_safe_validation_message(error),
        )
        for error in validation_error.errors()
    ]
    return problem_response(
        ProblemDetails(
            type="https://api.example.com/problems/validation-error",
            title="Invalid request",
            status=422,
            detail="One or more request fields are invalid.",
            code="VALIDATION_ERROR",
            instance=request.url.path,
            violations=violations,
        )
    )


async def unexpected_error_handler(request: Request, exc: Exception) -> JSONResponse:
    del exc
    return problem_response(
        ProblemDetails(
            type="https://api.example.com/problems/internal-error",
            title="Internal server error",
            status=500,
            detail="The service encountered an unexpected error.",
            code="INTERNAL_ERROR",
            instance=request.url.path,
        )
    )


def _safe_validation_message(error: dict[str, Any]) -> str:
    error_type = str(error.get("type", "invalid"))
    messages = {
        "missing": "This field is required.",
        "string_too_short": "This text is shorter than allowed.",
        "string_too_long": "This text is longer than allowed.",
    }
    return messages.get(error_type, "This value is invalid.")
