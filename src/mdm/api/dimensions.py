"""Company Dimension HTTP contracts and route composition."""

import base64
import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Path, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field, StrictStr

from mdm.api.auth import INVALID_ACCESS_TOKEN_RESPONSE
from mdm.api.authorization import AUTHORIZATION_DENIED_RESPONSE, build_authorization_guard
from mdm.api.schemas import ProblemDetails
from mdm.application.auth import HumanPrincipal
from mdm.application.authorization import AuthorizationAction, AuthorizationPolicy
from mdm.application.dimensions import (
    CompanyCursor,
    CompanyPage,
    CreateCompany,
    GetCompany,
    ListCompanies,
)
from mdm.domain.dimensions import CompanyValue, Dimension, DimensionValidationError

COMPANY_CURSOR_EXAMPLE = (
    "eyJpIjoiMDFhMDgzYzMtODhlOC03MTIzLTgwMDAtMDAwMDAwMDAwMDAxIiwidCI6"
    "IjIwMjYtMDktMDlUMDE6MjM6NDVaIiwidiI6MX0"
)
COMPANY_RESPONSE_EXAMPLE: dict[str, Any] = {
    "id": "01a083c3-88e8-7123-8000-000000000001",
    "code": "SAM01",
    "value": "SAMSUNG_ELECTRONICS",
    "version": 1,
    "created_at": "2026-09-09T01:23:45Z",
    "updated_at": "2026-09-09T01:23:45Z",
    "deleted_at": None,
}


class CompanyCreateRequest(BaseModel):
    """Company Dimension 생성에 필요한 업무 필드입니다."""

    model_config = ConfigDict(extra="forbid")

    code: Annotated[
        StrictStr,
        Field(
            min_length=1,
            max_length=32,
            description=(
                "MasterCode 합성에 사용할 Company 대표 코드입니다. ASCII 소문자는 대문자로 "
                "바꾸지만 공백과 A-Z·0-9 이외 문자는 거부하며, 정규화 후 N으로만 이루어진 "
                "1~32자 값은 예약 코드라서 사용할 수 없습니다."
            ),
            examples=["sam01"],
        ),
    ]
    value: Annotated[
        StrictStr,
        Field(
            min_length=1,
            description=(
                "정규화할 Company 이름입니다. 앞뒤 일반 공백을 제거하고 ASCII 소문자를 "
                "대문자로 바꾸며, 연속된 일반 공백과 밑줄을 밑줄 하나로 축약합니다. "
                "정규화 결과는 A-Z·0-9와 단어 사이 밑줄만 사용한 1~128자여야 하며 탭, "
                "줄바꿈, 비 ASCII 문자와 특수문자는 거부합니다."
            ),
            examples=["  Samsung   Electronics  "],
            json_schema_extra={
                "x-normalized-pattern": "^[A-Z0-9]+(?:_[A-Z0-9]+)*$",
                "x-normalized-minLength": 1,
                "x-normalized-maxLength": 128,
            },
        ),
    ]
    reason: Annotated[
        StrictStr | None,
        Field(
            description=(
                "생성 이유입니다. 앞뒤 일반 공백을 제거한 결과가 500자 이하여야 하며, "
                "빈 값은 저장하지 않습니다."
            ),
            examples=["신규 제조사 등록"],
            json_schema_extra={"x-normalized-maxLength": 500},
        ),
    ] = None


class CompanyResponse(BaseModel):
    """현재 Company Dimension 상태입니다."""

    model_config = ConfigDict(json_schema_extra={"example": COMPANY_RESPONSE_EXAMPLE})

    id: Annotated[UUID, Field(description="서버가 생성한 Company UUIDv7 식별자입니다.")]
    code: Annotated[str, Field(description="정규화된 Company 대표 코드입니다.", examples=["SAM"])]
    value: Annotated[
        str,
        Field(description="정규화된 Company 값입니다.", examples=["SAMSUNG_ELECTRONICS"]),
    ]
    version: Annotated[int, Field(description="현재 상태의 낙관적 잠금 버전입니다.", examples=[1])]
    created_at: Annotated[datetime, Field(description="Company 생성 시각입니다.")]
    updated_at: Annotated[datetime, Field(description="현재 상태가 마지막으로 변경된 시각입니다.")]
    deleted_at: Annotated[
        None,
        Field(
            description=(
                "논리 삭제 시각입니다. 현재 엔드포인트는 활성 Company만 반환하므로 항상 null입니다."
            )
        ),
    ]


class CompanyListResponse(BaseModel):
    """최신 생성 순서로 조회한 Company cursor 페이지입니다."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "items": [COMPANY_RESPONSE_EXAMPLE],
                "next_cursor": COMPANY_CURSOR_EXAMPLE,
            }
        }
    )

    items: Annotated[
        list[CompanyResponse], Field(description="현재 페이지의 활성 Company 목록입니다.")
    ]
    next_cursor: Annotated[
        str | None,
        Field(
            max_length=512,
            description=(
                "다음 페이지가 있을 때 전달되는 불투명 cursor입니다. 마지막 페이지이면 null입니다."
            ),
            examples=[COMPANY_CURSOR_EXAMPLE],
        ),
    ]


NOT_FOUND_RESPONSE: dict[str, Any] = {
    "model": ProblemDetails,
    "description": "활성 Company를 찾을 수 없습니다.",
    "content": {
        "application/problem+json": {
            "schema": {"$ref": "#/components/schemas/ProblemDetails"},
            "example": {
                "type": "/problems/dimension-not-found",
                "title": "Dimension을 찾을 수 없음",
                "status": 404,
                "detail": "요청한 활성 Company Dimension을 찾을 수 없습니다.",
                "code": "DIMENSION_NOT_FOUND",
            },
        }
    },
}
SERVICE_UNAVAILABLE_RESPONSE: dict[str, Any] = {
    "model": ProblemDetails,
    "description": "Company 데이터를 일시적으로 처리할 수 없습니다.",
    "content": {
        "application/problem+json": {
            "schema": {"$ref": "#/components/schemas/ProblemDetails"},
            "example": {
                "type": "/problems/service-unavailable",
                "title": "서비스를 사용할 수 없음",
                "status": 503,
                "detail": "요청을 일시적으로 처리할 수 없습니다. 잠시 후 다시 시도해 주세요.",
                "code": "SERVICE_UNAVAILABLE",
            },
        }
    },
}

CONFLICT_RESPONSE: dict[str, Any] = {
    "model": ProblemDetails,
    "description": "정규화된 Company code 또는 value가 이미 사용 중입니다.",
    "content": {
        "application/problem+json": {
            "schema": {"$ref": "#/components/schemas/ProblemDetails"},
            "examples": {
                "codeConflict": {
                    "summary": "대표 코드 중복",
                    "value": {
                        "type": "/problems/dimension-code-conflict",
                        "title": "Dimension 대표 코드 충돌",
                        "status": 409,
                        "detail": "정규화된 Company 대표 코드가 이미 사용 중입니다.",
                        "code": "DIMENSION_CODE_CONFLICT",
                    },
                },
                "valueConflict": {
                    "summary": "값 중복",
                    "value": {
                        "type": "/problems/dimension-value-conflict",
                        "title": "Dimension 값 충돌",
                        "status": 409,
                        "detail": "정규화된 Company 값이 이미 사용 중입니다.",
                        "code": "DIMENSION_VALUE_CONFLICT",
                    },
                },
                "multipleConflicts": {
                    "summary": "대표 코드와 값 중복",
                    "value": {
                        "type": "/problems/dimension-multiple-conflicts",
                        "title": "여러 Dimension 필드 충돌",
                        "status": 409,
                        "detail": "정규화된 Company 대표 코드와 값이 모두 이미 사용 중입니다.",
                        "code": "DIMENSION_MULTIPLE_CONFLICTS",
                    },
                },
            },
        }
    },
}


def _validation_response(*, description: str, field: str) -> dict[str, Any]:
    return {
        "model": ProblemDetails,
        "description": description,
        "content": {
            "application/problem+json": {
                "schema": {"$ref": "#/components/schemas/ProblemDetails"},
                "example": {
                    "type": "/problems/validation-error",
                    "title": "유효하지 않은 요청",
                    "status": 422,
                    "detail": "하나 이상의 요청 필드가 유효하지 않습니다.",
                    "code": "VALIDATION_ERROR",
                    "violations": [{"field": field, "message": "유효하지 않은 값입니다."}],
                },
            }
        },
    }


BODY_VALIDATION_RESPONSE = _validation_response(
    description="Company 생성 입력값이 유효하지 않습니다.", field="body.code"
)
PAGINATION_VALIDATION_RESPONSE = _validation_response(
    description="cursor 또는 limit가 유효하지 않습니다.", field="query.cursor"
)
IDENTIFIER_VALIDATION_RESPONSE = _validation_response(
    description="Company 식별자 형식이 유효하지 않습니다.", field="path.company_id"
)


def build_company_router(
    *,
    create_company: CreateCompany,
    get_company: GetCompany,
    list_companies: ListCompanies,
    principal_dependency: Callable[..., HumanPrincipal],
    authorization: AuthorizationPolicy,
) -> APIRouter:
    """Build Company routes from explicit application and authentication dependencies."""
    router = APIRouter(prefix="/dimensions/companies", tags=["Company Dimensions"])
    mutation_guard = build_authorization_guard(
        principal_dependency, authorization, AuthorizationAction.MUTATE_DATA
    )
    read_guard = build_authorization_guard(
        principal_dependency, authorization, AuthorizationAction.READ_DATA
    )

    @router.post(
        "",
        operation_id="create_company_dimension",
        response_model=CompanyResponse,
        status_code=status.HTTP_201_CREATED,
        summary="Company Dimension 생성",
        description=(
            "ADMIN 또는 SUPER_ADMIN이 Company 대표 코드와 값을 정규화해 생성합니다. "
            "인증 주체와 역할은 Bearer JWT에서만 가져옵니다."
        ),
        responses={
            status.HTTP_201_CREATED: {
                "description": "Company Dimension을 생성했습니다.",
                "headers": {
                    "ETag": {
                        "description": (
                            '정수 version N을 따옴표까지 포함한 강한 ETag "N"으로 표현합니다.'
                        ),
                        "schema": {"type": "string", "example": '"1"'},
                    }
                },
            },
            status.HTTP_401_UNAUTHORIZED: INVALID_ACCESS_TOKEN_RESPONSE,
            status.HTTP_403_FORBIDDEN: AUTHORIZATION_DENIED_RESPONSE,
            status.HTTP_409_CONFLICT: CONFLICT_RESPONSE,
            status.HTTP_422_UNPROCESSABLE_CONTENT: BODY_VALIDATION_RESPONSE,
            status.HTTP_503_SERVICE_UNAVAILABLE: SERVICE_UNAVAILABLE_RESPONSE,
        },
    )
    async def create_company_dimension(
        payload: CompanyCreateRequest,
        response: Response,
        principal: Annotated[HumanPrincipal, Depends(mutation_guard)],
    ) -> CompanyResponse:
        try:
            company = await create_company.execute(
                principal,
                code=payload.code,
                value=payload.value,
                reason=payload.reason,
            )
        except DimensionValidationError as error:
            raise DimensionValidationError(f"body.{error.field}", error.message) from error
        response.headers["ETag"] = _etag(company.version)
        return _company_response(company)

    @router.get(
        "",
        operation_id="list_company_dimensions",
        response_model=CompanyListResponse,
        status_code=status.HTTP_200_OK,
        summary="Company Dimension 목록 조회",
        description=(
            "활성 Company를 생성 시각 내림차순으로 조회하며, 생성 시각이 같으면 ID 내림차순으로 "
            "정렬합니다. next_cursor를 다음 요청의 cursor에 그대로 전달할 수 있으며 전체 항목 수는 "
            "제공하지 않습니다. 페이지마다 limit을 바꿀 수 있고 cursor는 만료되지 않습니다. "
            "페이지를 순회하는 동안 새로 생긴 항목은 이미 지난 앞쪽 구간에 위치하므로 뒤 페이지에 "
            "끼어들지 않으며, 각 페이지 요청 시점에 이미 삭제된 항목은 결과에서 제외됩니다."
        ),
        responses={
            status.HTTP_200_OK: {"description": "활성 Company Dimension 목록을 반환합니다."},
            status.HTTP_401_UNAUTHORIZED: INVALID_ACCESS_TOKEN_RESPONSE,
            status.HTTP_422_UNPROCESSABLE_CONTENT: PAGINATION_VALIDATION_RESPONSE,
            status.HTTP_503_SERVICE_UNAVAILABLE: SERVICE_UNAVAILABLE_RESPONSE,
        },
    )
    async def list_company_dimensions(
        principal: Annotated[HumanPrincipal, Depends(read_guard)],
        cursor: Annotated[
            str | None,
            Query(
                max_length=512,
                description="직전 응답의 next_cursor 값입니다.",
                examples=[COMPANY_CURSOR_EXAMPLE],
            ),
        ] = None,
        limit: Annotated[
            int,
            Query(
                ge=1,
                le=100,
                description="한 페이지에 반환할 항목 수입니다. 기본값은 50입니다.",
            ),
        ] = 50,
    ) -> CompanyListResponse:
        after = None if cursor is None else _decode_cursor(cursor)
        page = await list_companies.execute(principal, after=after, limit=limit)
        return _page_response(page)

    @router.get(
        "/{company_id}",
        operation_id="get_company_dimension",
        response_model=CompanyResponse,
        status_code=status.HTTP_200_OK,
        summary="Company Dimension 단건 조회",
        description=(
            "UUID로 활성 Company를 조회하고 본문의 version과 같은 강한 ETag를 반환합니다."
        ),
        responses={
            status.HTTP_200_OK: {
                "description": "활성 Company Dimension을 반환합니다.",
                "headers": {
                    "ETag": {
                        "description": (
                            '정수 version N을 따옴표까지 포함한 강한 ETag "N"으로 표현합니다.'
                        ),
                        "schema": {"type": "string", "example": '"1"'},
                    }
                },
            },
            status.HTTP_401_UNAUTHORIZED: INVALID_ACCESS_TOKEN_RESPONSE,
            status.HTTP_404_NOT_FOUND: NOT_FOUND_RESPONSE,
            status.HTTP_422_UNPROCESSABLE_CONTENT: IDENTIFIER_VALIDATION_RESPONSE,
            status.HTTP_503_SERVICE_UNAVAILABLE: SERVICE_UNAVAILABLE_RESPONSE,
        },
    )
    async def get_company_dimension(
        company_id: Annotated[
            UUID, Path(description="조회할 Company Dimension의 UUID 식별자입니다.")
        ],
        response: Response,
        principal: Annotated[HumanPrincipal, Depends(read_guard)],
    ) -> CompanyResponse:
        company = await get_company.execute(principal, company_id)
        response.headers["ETag"] = _etag(company.version)
        return _company_response(company)

    return router


def _company_response(company: Dimension[CompanyValue]) -> CompanyResponse:
    if company.deleted_at is not None:
        raise ValueError("inactive Company cannot be serialized")
    return CompanyResponse(
        id=company.id,
        code=company.code.value,
        value=company.value.value,
        version=company.version,
        created_at=company.created_at,
        updated_at=company.updated_at,
        deleted_at=company.deleted_at,
    )


def _page_response(page: CompanyPage) -> CompanyListResponse:
    next_cursor = None
    if page.has_more and page.items:
        last = page.items[-1]
        next_cursor = _encode_cursor(CompanyCursor(created_at=last.created_at, id=last.id))
    return CompanyListResponse(
        items=[_company_response(company) for company in page.items],
        next_cursor=next_cursor,
    )


def _etag(version: int) -> str:
    return f'"{version}"'


def _encode_cursor(cursor: CompanyCursor) -> str:
    payload = json.dumps(
        {
            "v": 1,
            "t": cursor.created_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            "i": str(cursor.id),
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")


def _decode_cursor(encoded: str) -> CompanyCursor:
    try:
        padding = "=" * (-len(encoded) % 4)
        raw = base64.b64decode(encoded + padding, altchars=b"-_", validate=True)
        payload = json.loads(raw)
        if not isinstance(payload, dict) or set(payload) != {"v", "t", "i"}:
            raise ValueError
        if payload["v"] != 1 or not isinstance(payload["t"], str):
            raise ValueError
        if not isinstance(payload["i"], str):
            raise ValueError
        timestamp = datetime.fromisoformat(payload["t"].replace("Z", "+00:00"))
        return CompanyCursor(created_at=timestamp, id=UUID(payload["i"]))
    except (UnicodeError, ValueError, TypeError, json.JSONDecodeError):
        raise DimensionValidationError("query.cursor", "유효하지 않은 cursor입니다.") from None
