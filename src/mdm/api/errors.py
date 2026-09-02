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
            title="서비스를 사용할 수 없음",
            status=503,
            detail=("필수 의존 서비스를 사용할 수 없어 현재 요청을 처리할 수 없습니다."),
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
            title="유효하지 않은 요청",
            status=422,
            detail="하나 이상의 요청 필드가 유효하지 않습니다.",
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
            title="서버 내부 오류",
            status=500,
            detail="서비스에서 예상하지 못한 오류가 발생했습니다.",
            code="INTERNAL_ERROR",
            instance=request.url.path,
        )
    )


def _safe_validation_message(error: dict[str, Any]) -> str:
    error_type = str(error.get("type", "invalid"))
    messages = {
        "missing": "필수 필드입니다.",
        "string_too_short": "허용된 길이보다 짧은 문자열입니다.",
        "string_too_long": "허용된 길이보다 긴 문자열입니다.",
    }
    return messages.get(error_type, "유효하지 않은 값입니다.")
