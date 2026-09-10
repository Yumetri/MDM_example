"""Consistent translation of internal failures to public problem details."""

from collections.abc import Mapping
from typing import Any, cast

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from mdm.api.schemas import FieldViolation, ProblemDetails
from mdm.application.auth import InvalidAccessToken
from mdm.application.authorization import AuthorizationDenied
from mdm.application.dimensions import (
    CompanyCodeConflict,
    CompanyMultipleConflicts,
    CompanyNotFound,
    CompanyRepositoryUnavailable,
    CompanyValueConflict,
)
from mdm.application.health import ReadinessUnavailable
from mdm.application.memory_dimensions import (
    MemoryDimensionCodeConflict,
    MemoryDimensionMultipleConflicts,
    MemoryDimensionNotFound,
    MemoryDimensionRepositoryUnavailable,
    MemoryDimensionValueConflict,
)
from mdm.application.numeric_dimensions import (
    NumericDimensionCodeConflict,
    NumericDimensionMultipleConflicts,
    NumericDimensionNotFound,
    NumericDimensionRepositoryUnavailable,
    NumericDimensionValueConflict,
)
from mdm.application.preconditions import InvalidIfMatch, PreconditionFailed, PreconditionRequired
from mdm.application.string_dimensions import (
    StringDimensionCodeConflict,
    StringDimensionMultipleConflicts,
    StringDimensionNotFound,
    StringDimensionRepositoryUnavailable,
    StringDimensionValueConflict,
)
from mdm.domain.dimensions import DimensionValidationError


def problem_response(
    problem: ProblemDetails,
    *,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=problem.status,
        content=problem.model_dump(mode="json", exclude_none=True),
        media_type="application/problem+json",
        headers=headers,
    )


async def invalid_access_token_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, InvalidAccessToken)
    return problem_response(
        ProblemDetails(
            type="/problems/invalid-access-token",
            title="유효하지 않은 액세스 토큰",
            status=401,
            detail="유효한 Bearer 액세스 토큰이 필요합니다.",
            code="INVALID_ACCESS_TOKEN",
            instance=request.url.path,
        ),
        headers={"WWW-Authenticate": "Bearer"},
    )


async def authorization_denied_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, AuthorizationDenied)
    return problem_response(
        ProblemDetails(
            type="/problems/authorization-denied",
            title="권한 없음",
            status=403,
            detail="현재 역할로는 이 작업을 수행할 수 없습니다.",
            code="AUTHORIZATION_DENIED",
            instance=request.url.path,
        )
    )


async def readiness_unavailable_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, ReadinessUnavailable)
    return problem_response(
        ProblemDetails(
            type="/problems/service-unavailable",
            title="서비스를 사용할 수 없음",
            status=503,
            detail="데이터베이스 연결을 확인할 수 없어 현재 요청을 처리할 수 없습니다.",
            code="SERVICE_UNAVAILABLE",
            instance=request.url.path,
        )
    )


async def dimension_validation_error_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, DimensionValidationError)
    return problem_response(
        ProblemDetails(
            type="/problems/validation-error",
            title="유효하지 않은 요청",
            status=422,
            detail="하나 이상의 요청 필드가 유효하지 않습니다.",
            code="VALIDATION_ERROR",
            instance=request.url.path,
            violations=[FieldViolation(field=exc.field, message=exc.message)],
        )
    )


async def precondition_required_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, PreconditionRequired)
    return problem_response(
        ProblemDetails(
            type="/problems/precondition-required",
            title="사전 조건 필요",
            status=428,
            detail="Dimension을 변경하려면 직전 단건 응답의 ETag를 If-Match로 제공해야 합니다.",
            code="PRECONDITION_REQUIRED",
            instance=request.url.path,
        )
    )


async def invalid_if_match_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, InvalidIfMatch)
    return problem_response(
        ProblemDetails(
            type="/problems/invalid-if-match",
            title="유효하지 않은 If-Match",
            status=400,
            detail="If-Match에는 따옴표로 감싼 강한 정수 ETag 하나를 입력해야 합니다.",
            code="INVALID_IF_MATCH",
            instance=request.url.path,
        )
    )


async def precondition_failed_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, PreconditionFailed)
    return problem_response(
        ProblemDetails(
            type="/problems/precondition-failed",
            title="사전 조건 불일치",
            status=412,
            detail="Dimension이 조회 이후 변경되었습니다. 최신 상태와 ETag를 다시 조회해 주세요.",
            code="PRECONDITION_FAILED",
            instance=request.url.path,
        )
    )


async def company_not_found_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, CompanyNotFound)
    return problem_response(
        ProblemDetails(
            type="/problems/dimension-not-found",
            title="Dimension을 찾을 수 없음",
            status=404,
            detail="요청한 활성 Company Dimension을 찾을 수 없습니다.",
            code="DIMENSION_NOT_FOUND",
            instance=request.url.path,
        )
    )


async def company_repository_unavailable_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, CompanyRepositoryUnavailable)
    return problem_response(
        ProblemDetails(
            type="/problems/service-unavailable",
            title="서비스를 사용할 수 없음",
            status=503,
            detail="요청을 일시적으로 처리할 수 없습니다. 잠시 후 다시 시도해 주세요.",
            code="SERVICE_UNAVAILABLE",
            instance=request.url.path,
        )
    )


async def company_conflict_handler(request: Request, exc: Exception) -> JSONResponse:
    if isinstance(exc, CompanyCodeConflict):
        slug, title, detail, code, violations = (
            "dimension-code-conflict",
            "Dimension 대표 코드 충돌",
            "정규화된 Company 대표 코드가 이미 사용 중입니다.",
            "DIMENSION_CODE_CONFLICT",
            [FieldViolation(field="body.code", message="이미 사용 중인 값입니다.")],
        )
    elif isinstance(exc, CompanyValueConflict):
        slug, title, detail, code, violations = (
            "dimension-value-conflict",
            "Dimension 값 충돌",
            "정규화된 Company 값이 이미 사용 중입니다.",
            "DIMENSION_VALUE_CONFLICT",
            [FieldViolation(field="body.value", message="이미 사용 중인 값입니다.")],
        )
    else:
        assert isinstance(exc, CompanyMultipleConflicts)
        slug, title, detail, code, violations = (
            "dimension-multiple-conflicts",
            "여러 Dimension 필드 충돌",
            "정규화된 Company 대표 코드와 값이 모두 이미 사용 중입니다.",
            "DIMENSION_MULTIPLE_CONFLICTS",
            [
                FieldViolation(field="body.code", message="이미 사용 중인 값입니다."),
                FieldViolation(field="body.value", message="이미 사용 중인 값입니다."),
            ],
        )
    return problem_response(
        ProblemDetails(
            type=f"/problems/{slug}",
            title=title,
            status=409,
            detail=detail,
            code=code,
            instance=request.url.path,
            violations=violations,
        )
    )


async def string_dimension_not_found_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StringDimensionNotFound)
    return problem_response(
        ProblemDetails(
            type="/problems/dimension-not-found",
            title="Dimension을 찾을 수 없음",
            status=404,
            detail=f"요청한 활성 {exc.dimension_name} Dimension을 찾을 수 없습니다.",
            code="DIMENSION_NOT_FOUND",
            instance=request.url.path,
        )
    )


async def string_dimension_repository_unavailable_handler(
    request: Request, exc: Exception
) -> JSONResponse:
    assert isinstance(exc, StringDimensionRepositoryUnavailable)
    return problem_response(
        ProblemDetails(
            type="/problems/service-unavailable",
            title="서비스를 사용할 수 없음",
            status=503,
            detail="요청을 일시적으로 처리할 수 없습니다. 잠시 후 다시 시도해 주세요.",
            code="SERVICE_UNAVAILABLE",
            instance=request.url.path,
        )
    )


async def string_dimension_conflict_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(
        exc,
        (
            StringDimensionCodeConflict,
            StringDimensionValueConflict,
            StringDimensionMultipleConflicts,
        ),
    )
    name = exc.dimension_name
    if isinstance(exc, StringDimensionCodeConflict):
        slug, title, detail, code, violations = (
            "dimension-code-conflict",
            "Dimension 대표 코드 충돌",
            f"정규화된 {name} 대표 코드가 이미 사용 중입니다.",
            "DIMENSION_CODE_CONFLICT",
            [FieldViolation(field="body.code", message="이미 사용 중인 값입니다.")],
        )
    elif isinstance(exc, StringDimensionValueConflict):
        slug, title, detail, code, violations = (
            "dimension-value-conflict",
            "Dimension 값 충돌",
            f"정규화된 {name} 값이 이미 사용 중입니다.",
            "DIMENSION_VALUE_CONFLICT",
            [FieldViolation(field="body.value", message="이미 사용 중인 값입니다.")],
        )
    else:
        slug, title, detail, code, violations = (
            "dimension-multiple-conflicts",
            "여러 Dimension 필드 충돌",
            f"정규화된 {name} 대표 코드와 값이 모두 이미 사용 중입니다.",
            "DIMENSION_MULTIPLE_CONFLICTS",
            [
                FieldViolation(field="body.code", message="이미 사용 중인 값입니다."),
                FieldViolation(field="body.value", message="이미 사용 중인 값입니다."),
            ],
        )
    return problem_response(
        ProblemDetails(
            type=f"/problems/{slug}",
            title=title,
            status=409,
            detail=detail,
            code=code,
            instance=request.url.path,
            violations=violations,
        )
    )


async def numeric_dimension_not_found_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, NumericDimensionNotFound)
    return problem_response(
        ProblemDetails(
            type="/problems/dimension-not-found",
            title="Dimension을 찾을 수 없음",
            status=404,
            detail=f"요청한 활성 {exc.dimension_name} Dimension을 찾을 수 없습니다.",
            code="DIMENSION_NOT_FOUND",
            instance=request.url.path,
        )
    )


async def numeric_dimension_repository_unavailable_handler(
    request: Request, exc: Exception
) -> JSONResponse:
    assert isinstance(exc, NumericDimensionRepositoryUnavailable)
    return problem_response(
        ProblemDetails(
            type="/problems/service-unavailable",
            title="서비스를 사용할 수 없음",
            status=503,
            detail="요청을 일시적으로 처리할 수 없습니다. 잠시 후 다시 시도해 주세요.",
            code="SERVICE_UNAVAILABLE",
            instance=request.url.path,
        )
    )


async def numeric_dimension_conflict_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(
        exc,
        (
            NumericDimensionCodeConflict,
            NumericDimensionValueConflict,
            NumericDimensionMultipleConflicts,
        ),
    )
    name = exc.dimension_name
    if isinstance(exc, NumericDimensionCodeConflict):
        slug, title, detail, code, violations = (
            "dimension-code-conflict",
            "Dimension 대표 코드 충돌",
            f"정규화된 {name} 대표 코드가 이미 사용 중입니다.",
            "DIMENSION_CODE_CONFLICT",
            [FieldViolation(field="body.code", message="이미 사용 중인 값입니다.")],
        )
    elif isinstance(exc, NumericDimensionValueConflict):
        slug, title, detail, code, violations = (
            "dimension-value-conflict",
            "Dimension 값 충돌",
            f"{name} 값이 이미 사용 중입니다.",
            "DIMENSION_VALUE_CONFLICT",
            [FieldViolation(field="body.value", message="이미 사용 중인 값입니다.")],
        )
    else:
        slug, title, detail, code, violations = (
            "dimension-multiple-conflicts",
            "여러 Dimension 필드 충돌",
            f"{name} 대표 코드와 값이 모두 이미 사용 중입니다.",
            "DIMENSION_MULTIPLE_CONFLICTS",
            [
                FieldViolation(field="body.code", message="이미 사용 중인 값입니다."),
                FieldViolation(field="body.value", message="이미 사용 중인 값입니다."),
            ],
        )
    return problem_response(
        ProblemDetails(
            type=f"/problems/{slug}",
            title=title,
            status=409,
            detail=detail,
            code=code,
            instance=request.url.path,
            violations=violations,
        )
    )


async def memory_dimension_not_found_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, MemoryDimensionNotFound)
    return problem_response(
        ProblemDetails(
            type="/problems/dimension-not-found",
            title="Dimension을 찾을 수 없음",
            status=404,
            detail="요청한 활성 Memory Dimension을 찾을 수 없습니다.",
            code="DIMENSION_NOT_FOUND",
            instance=request.url.path,
        )
    )


async def memory_dimension_repository_unavailable_handler(
    request: Request, exc: Exception
) -> JSONResponse:
    assert isinstance(exc, MemoryDimensionRepositoryUnavailable)
    return problem_response(
        ProblemDetails(
            type="/problems/service-unavailable",
            title="서비스를 사용할 수 없음",
            status=503,
            detail="요청을 일시적으로 처리할 수 없습니다. 잠시 후 다시 시도해 주세요.",
            code="SERVICE_UNAVAILABLE",
            instance=request.url.path,
        )
    )


async def memory_dimension_conflict_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(
        exc,
        (
            MemoryDimensionCodeConflict,
            MemoryDimensionValueConflict,
            MemoryDimensionMultipleConflicts,
        ),
    )
    if isinstance(exc, MemoryDimensionCodeConflict):
        slug, title, detail, code, violations = (
            "dimension-code-conflict",
            "Dimension 대표 코드 충돌",
            "정규화된 Memory 대표 코드가 이미 사용 중입니다.",
            "DIMENSION_CODE_CONFLICT",
            [FieldViolation(field="body.code", message="이미 사용 중인 값입니다.")],
        )
    elif isinstance(exc, MemoryDimensionValueConflict):
        slug, title, detail, code, violations = (
            "dimension-value-conflict",
            "Dimension 값 충돌",
            "동등한 Memory 용량이 이미 사용 중입니다.",
            "DIMENSION_VALUE_CONFLICT",
            [FieldViolation(field="body.value", message="이미 사용 중인 값입니다.")],
        )
    else:
        slug, title, detail, code, violations = (
            "dimension-multiple-conflicts",
            "여러 Dimension 필드 충돌",
            "Memory 대표 코드와 동등한 용량이 모두 이미 사용 중입니다.",
            "DIMENSION_MULTIPLE_CONFLICTS",
            [
                FieldViolation(field="body.code", message="이미 사용 중인 값입니다."),
                FieldViolation(field="body.value", message="이미 사용 중인 값입니다."),
            ],
        )
    return problem_response(
        ProblemDetails(
            type=f"/problems/{slug}",
            title=title,
            status=409,
            detail=detail,
            code=code,
            instance=request.url.path,
            violations=violations,
        )
    )


async def validation_error_handler(request: Request, exc: Exception) -> JSONResponse:
    validation_error = cast(RequestValidationError, exc)
    if request.method == "PATCH" and "if-match" not in request.headers:
        return await precondition_required_handler(request, PreconditionRequired())
    violations = [
        FieldViolation(
            field=".".join(str(part) for part in error["loc"]),
            message=_safe_validation_message(error),
        )
        for error in validation_error.errors()
    ]
    return problem_response(
        ProblemDetails(
            type="/problems/validation-error",
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
            type="/problems/internal-error",
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
