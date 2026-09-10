"""HTTP contract for Memory Dimensions."""

import base64
import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Path, Query, Request, Response, status
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StrictStr,
    model_validator,
)

from mdm.api.auth import INVALID_ACCESS_TOKEN_RESPONSE
from mdm.api.authorization import AUTHORIZATION_DENIED_RESPONSE, build_authorization_guard
from mdm.api.preconditions import (
    DimensionIfMatchHeader,
    dimension_precondition_responses,
    parse_dimension_if_match_values,
)
from mdm.api.routes import API_V1_PREFIX
from mdm.api.schemas import ProblemDetails
from mdm.application.auth import HumanPrincipal
from mdm.application.authorization import AuthorizationAction, AuthorizationPolicy
from mdm.application.memory_dimensions import (
    CreateMemory,
    GetMemory,
    ListMemories,
    MemoryDimensionCursor,
    MemoryDimensionPage,
    UpdateMemoryValue,
)
from mdm.domain.dimensions import Dimension, DimensionValidationError, MemoryValue

CURSOR_EXAMPLE = (
    "eyJpIjoiMDFhMDgzYzMtODhlOC03MTIzLTgwMDAtMDAwMDAwMDAwMDMxIiwidCI6"
    "IjIwMjYtMDktMDlUMDE6MjM6NDVaIiwidiI6MX0"
)
MEMORY_VALUE_EXAMPLE = {"amount": 128, "unit": "GB", "capacity_mb": 128_000}
MEMORY_EXAMPLE: dict[str, Any] = {
    "id": "01a083c3-88e8-7123-8000-000000000031",
    "code": "MEM128",
    "value": MEMORY_VALUE_EXAMPLE,
    "version": 1,
    "created_at": "2026-09-09T01:23:45Z",
    "updated_at": "2026-09-09T01:23:45Z",
    "deleted_at": None,
}


def _code_field() -> Any:
    return Field(
        min_length=1,
        max_length=32,
        description=(
            "MasterCode 합성에 사용할 대표 코드입니다. ASCII 소문자는 대문자로 바꾸지만 "
            "공백과 A-Z·0-9 이외 문자는 거부하며, 정규화 후 N으로만 이루어진 1~32자 값은 "
            "예약 코드라서 사용할 수 없습니다."
        ),
        examples=["mem128"],
    )


def _reason_field() -> Any:
    return Field(
        description=(
            "생성 이유입니다. 앞뒤 일반 공백을 제거한 결과가 500자 이하여야 하며, "
            "빈 값은 저장하지 않습니다."
        ),
        examples=["신규 메모리 용량 등록"],
        json_schema_extra={"x-normalized-maxLength": 500},
    )


class MemoryValueInput(BaseModel):
    """Memory Dimension의 생성·수정 입력 값입니다."""

    model_config = ConfigDict(extra="forbid")
    amount: Annotated[
        StrictInt,
        Field(
            ge=1,
            le=2_147_483_647,
            description=(
                "문자열·boolean·실수 변환을 허용하지 않는 1~2147483647의 JSON 정수입니다."
            ),
            examples=[128],
        ),
    ]
    unit: Annotated[
        StrictStr,
        Field(
            pattern=r"^ *(?:[Mm][Bb]|[Gg][Bb]|[Tt][Bb]|[Pp][Bb]) *$",
            description=(
                "십진 SI 단위 MB, GB, TB, PB 중 하나입니다. 앞뒤 일반 공백은 제거하고 "
                "ASCII 소문자는 대문자로 바꾸며 탭·줄바꿈·제어문자는 거부합니다."
            ),
            examples=["gb"],
        ),
    ]


class MemoryCreateRequest(BaseModel):
    """Memory Dimension 생성 입력입니다."""

    model_config = ConfigDict(extra="forbid")
    code: Annotated[StrictStr, _code_field()]
    value: Annotated[
        MemoryValueInput,
        Field(
            description=(
                "메모리 양과 십진 단위를 묶은 논리 값입니다. capacity_mb는 서버가 생성하므로 "
                "입력할 수 없습니다."
            )
        ),
    ]
    reason: Annotated[StrictStr | None, _reason_field()] = None


class MemoryValueUpdateRequest(BaseModel):
    """Memory Dimension code 또는 value 수정 입력입니다."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={"anyOf": [{"required": ["code"]}, {"required": ["value"]}]},
    )
    code: Annotated[StrictStr, _code_field()] = None  # type: ignore[assignment]
    value: Annotated[
        MemoryValueInput,
        Field(
            description=(
                "새 amount·unit을 묶은 논리 값입니다. 현재 값과 동등한 capacity_mb이면 현재 "
                "저장된 amount·unit 표현을 유지하고 상태를 변경하지 않습니다."
            )
        ),
    ] = None  # type: ignore[assignment]
    reason: Annotated[
        StrictStr | None,
        Field(
            description=(
                "수정 이유입니다. 앞뒤 일반 공백을 제거한 결과가 500자 이하여야 하며, "
                "빈 값은 저장하지 않습니다."
            ),
            examples=["용량 표기 정정"],
            json_schema_extra={"x-normalized-maxLength": 500},
        ),
    ] = None

    @model_validator(mode="after")
    def require_business_field(self) -> "MemoryValueUpdateRequest":
        if not self.model_fields_set.intersection({"code", "value"}):
            raise ValueError("code 또는 value 중 하나 이상을 입력해야 합니다.")
        return self


class MemoryValueResponse(BaseModel):
    """현재 단위 표현과 서버가 계산한 동등 용량입니다."""

    amount: Annotated[
        int,
        Field(
            ge=1,
            le=2_147_483_647,
            description="현재 저장된 1~2147483647의 정수입니다.",
        ),
    ]
    unit: Annotated[
        Literal["MB", "GB", "TB", "PB"],
        Field(description="정규화해 보존한 십진 SI 단위입니다."),
    ]
    capacity_mb: Annotated[
        int,
        Field(
            ge=1,
            le=2_147_483_647_000_000_000,
            description=(
                "amount와 unit에서 서버가 계산한 MB 기준 동등 용량이며 중복 판정에 사용합니다."
            ),
        ),
    ]


class MemoryResponse(BaseModel):
    """현재 Memory Dimension 상태입니다."""

    model_config = ConfigDict(json_schema_extra={"example": MEMORY_EXAMPLE})
    id: Annotated[UUID, Field(description="서버가 생성한 UUIDv7 식별자입니다.")]
    code: Annotated[str, Field(description="정규화된 대표 코드입니다.")]
    value: Annotated[
        MemoryValueResponse,
        Field(description="원래 amount·unit과 생성된 capacity_mb를 포함한 Memory 값입니다."),
    ]
    version: Annotated[int, Field(description="현재 상태의 낙관적 잠금 버전입니다.")]
    created_at: Annotated[datetime, Field(description="생성 시각입니다.")]
    updated_at: Annotated[datetime, Field(description="현재 상태가 마지막으로 변경된 시각입니다.")]
    deleted_at: Annotated[
        None,
        Field(description="활성 Dimension만 반환하는 현재 엔드포인트에서는 항상 null입니다."),
    ]


class MemoryListResponse(BaseModel):
    """최신 생성 순서로 조회한 Memory cursor 페이지입니다."""

    model_config = ConfigDict(
        json_schema_extra={"example": {"items": [MEMORY_EXAMPLE], "next_cursor": CURSOR_EXAMPLE}}
    )
    items: Annotated[
        list[MemoryResponse], Field(description="현재 페이지의 활성 Memory 목록입니다.")
    ]
    next_cursor: Annotated[
        str | None,
        Field(
            max_length=512,
            description="다음 페이지가 있으면 전달되는 불투명 cursor이며 마지막이면 null입니다.",
            examples=[CURSOR_EXAMPLE],
        ),
    ]


def build_memory_router(
    *,
    create_memory: CreateMemory,
    get_memory: GetMemory,
    list_memories: ListMemories,
    update_memory: UpdateMemoryValue,
    principal_dependency: Callable[..., HumanPrincipal],
    authorization: AuthorizationPolicy,
) -> APIRouter:
    router = APIRouter(
        prefix=f"{API_V1_PREFIX}/dimensions/memories",
        tags=["Memory Dimensions"],
    )
    mutation_guard = build_authorization_guard(
        principal_dependency, authorization, AuthorizationAction.MUTATE_DATA
    )
    read_guard = build_authorization_guard(
        principal_dependency, authorization, AuthorizationAction.READ_DATA
    )

    @router.post(
        "",
        operation_id="create_memory_dimension",
        response_model=MemoryResponse,
        status_code=status.HTTP_201_CREATED,
        summary="Memory Dimension 생성",
        description=(
            "ADMIN 또는 SUPER_ADMIN이 Memory 대표 코드와 amount·unit 값을 생성합니다. "
            "capacity_mb는 십진 단위로 서버가 계산하며 입력할 수 없습니다. "
            "인증 주체와 역할은 Bearer JWT에서만 가져옵니다."
        ),
        responses=_create_responses(),
    )
    async def create_memory_route(
        payload: MemoryCreateRequest,
        response: Response,
        principal: Annotated[HumanPrincipal, Depends(mutation_guard)],
    ) -> MemoryResponse:
        try:
            dimension = await create_memory.execute(
                principal,
                code=payload.code,
                amount=payload.value.amount,
                unit=payload.value.unit,
                reason=payload.reason,
            )
        except DimensionValidationError as error:
            raise DimensionValidationError(f"body.{error.field}", error.message) from error
        response.headers["ETag"] = _etag(dimension.version)
        return _response(dimension)

    @router.get(
        "",
        operation_id="list_memory_dimensions",
        response_model=MemoryListResponse,
        status_code=status.HTTP_200_OK,
        summary="Memory Dimension 목록 조회",
        description=(
            "활성 Memory를 생성 시각과 ID의 내림차순으로 조회합니다. next_cursor를 다음 요청에 "
            "그대로 전달하며 전체 항목 수는 제공하지 않습니다. 페이지마다 limit을 바꿀 수 있고 "
            "cursor는 만료되지 않습니다. 페이지를 순회하는 동안 새로 생긴 항목은 이미 지난 앞쪽 "
            "구간에 위치하므로 뒤 페이지에 끼어들지 않으며, 각 페이지 요청 시점에 이미 삭제된 "
            "항목은 결과에서 제외됩니다."
        ),
        responses=_list_responses(),
    )
    async def list_memory_route(
        principal: Annotated[HumanPrincipal, Depends(read_guard)],
        cursor: Annotated[
            str | None,
            Query(
                max_length=512,
                description="직전 응답의 next_cursor 값입니다.",
                examples=[CURSOR_EXAMPLE],
            ),
        ] = None,
        limit: Annotated[
            int,
            Query(ge=1, le=100, description="한 페이지에 반환할 항목 수입니다. 기본값은 50입니다."),
        ] = 50,
    ) -> MemoryListResponse:
        after = None if cursor is None else _decode_cursor(cursor)
        page = await list_memories.execute(principal, after=after, limit=limit)
        return _page_response(page)

    @router.get(
        "/{dimension_id}",
        operation_id="get_memory_dimension",
        response_model=MemoryResponse,
        status_code=status.HTTP_200_OK,
        summary="Memory Dimension 단건 조회",
        description=("UUID로 활성 Memory를 조회하고 본문의 version과 같은 강한 ETag를 반환합니다."),
        responses=_get_responses(),
    )
    async def get_memory_route(
        dimension_id: Annotated[
            UUID,
            Path(description="조회할 Memory Dimension의 UUID 식별자입니다."),
        ],
        response: Response,
        principal: Annotated[HumanPrincipal, Depends(read_guard)],
    ) -> MemoryResponse:
        dimension = await get_memory.execute(principal, dimension_id)
        response.headers["ETag"] = _etag(dimension.version)
        return _response(dimension)

    @router.patch(
        "/{dimension_id}",
        operation_id="update_memory_dimension_value",
        response_model=MemoryResponse,
        status_code=status.HTTP_200_OK,
        summary="Memory Dimension 코드·값 수정",
        description=(
            "ADMIN 또는 SUPER_ADMIN이 직전 단건 응답의 강한 ETag를 If-Match로 제공해 Memory "
            "code 또는 amount·unit value를 조건부 수정합니다. code가 실제로 바뀌면 이를 참조하는 "
            "활성·삭제 MasterCode를 같은 트랜잭션에서 재합성합니다. 현재와 동등한 capacity_mb를 "
            "만드는 value와 현재와 동일한 code만 제공하면 저장된 표현과 version·시각·감사 로그를 "
            "유지합니다."
        ),
        responses=_update_responses(),
    )
    async def update_memory_route(
        dimension_id: Annotated[
            UUID, Path(description="수정할 Memory Dimension의 UUID 식별자입니다.")
        ],
        payload: MemoryValueUpdateRequest,
        request: Request,
        response: Response,
        principal: Annotated[HumanPrincipal, Depends(mutation_guard)],
        if_match: DimensionIfMatchHeader,
    ) -> MemoryResponse:
        expected_version = parse_dimension_if_match_values(request.headers.getlist("if-match"))
        try:
            dimension = await update_memory.execute(
                principal,
                dimension_id,
                expected_version=expected_version,
                amount=None if payload.value is None else payload.value.amount,
                unit=None if payload.value is None else payload.value.unit,
                code=payload.code,
                reason=payload.reason,
            )
        except DimensionValidationError as error:
            raise DimensionValidationError(f"body.{error.field}", error.message) from error
        response.headers["ETag"] = _etag(dimension.version)
        return _response(dimension)

    return router


def _response(dimension: Dimension[MemoryValue]) -> MemoryResponse:
    if dimension.deleted_at is not None:
        raise ValueError("inactive Memory Dimension cannot be serialized")
    return MemoryResponse(
        id=dimension.id,
        code=dimension.code.value,
        value=MemoryValueResponse(
            amount=dimension.value.amount,
            unit=dimension.value.unit.value,
            capacity_mb=dimension.value.capacity_mb,
        ),
        version=dimension.version,
        created_at=dimension.created_at,
        updated_at=dimension.updated_at,
        deleted_at=dimension.deleted_at,
    )


def _page_response(page: MemoryDimensionPage) -> MemoryListResponse:
    next_cursor = None
    if page.has_more and page.items:
        last = page.items[-1]
        next_cursor = _encode_cursor(MemoryDimensionCursor(created_at=last.created_at, id=last.id))
    return MemoryListResponse(
        items=[_response(item) for item in page.items],
        next_cursor=next_cursor,
    )


def _etag(version: int) -> str:
    return f'"{version}"'


def _encode_cursor(cursor: MemoryDimensionCursor) -> str:
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


def _decode_cursor(encoded: str) -> MemoryDimensionCursor:
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
        return MemoryDimensionCursor(created_at=timestamp, id=UUID(payload["i"]))
    except (UnicodeError, ValueError, TypeError, json.JSONDecodeError):
        raise DimensionValidationError("query.cursor", "유효하지 않은 cursor입니다.") from None


def _problem_response(*, description: str, example: dict[str, Any]) -> dict[str, Any]:
    return {
        "model": ProblemDetails,
        "description": description,
        "content": {
            "application/problem+json": {
                "schema": {"$ref": "#/components/schemas/ProblemDetails"},
                "example": example,
            }
        },
    }


def _validation_response(*, description: str, field: str) -> dict[str, Any]:
    return _problem_response(
        description=description,
        example={
            "type": "/problems/validation-error",
            "title": "유효하지 않은 요청",
            "status": 422,
            "detail": "하나 이상의 요청 필드가 유효하지 않습니다.",
            "code": "VALIDATION_ERROR",
            "violations": [{"field": field, "message": "유효하지 않은 값입니다."}],
        },
    )


def _service_unavailable_response() -> dict[str, Any]:
    return _problem_response(
        description="Memory 데이터를 일시적으로 처리할 수 없습니다.",
        example={
            "type": "/problems/service-unavailable",
            "title": "서비스를 사용할 수 없음",
            "status": 503,
            "detail": "요청을 일시적으로 처리할 수 없습니다. 잠시 후 다시 시도해 주세요.",
            "code": "SERVICE_UNAVAILABLE",
        },
    )


def _conflict_response() -> dict[str, Any]:
    response = _problem_response(
        description="정규화된 Memory code 또는 동등한 capacity_mb가 이미 사용 중입니다.",
        example={},
    )
    content = response["content"]["application/problem+json"]
    content.pop("example")
    content["examples"] = {
        "codeConflict": {
            "summary": "대표 코드 중복",
            "value": {
                "type": "/problems/dimension-code-conflict",
                "title": "Dimension 대표 코드 충돌",
                "status": 409,
                "detail": "정규화된 Memory 대표 코드가 이미 사용 중입니다.",
                "code": "DIMENSION_CODE_CONFLICT",
                "violations": [{"field": "body.code", "message": "이미 사용 중인 값입니다."}],
            },
        },
        "valueConflict": {
            "summary": "동등 용량 중복",
            "value": {
                "type": "/problems/dimension-value-conflict",
                "title": "Dimension 값 충돌",
                "status": 409,
                "detail": "동등한 Memory 용량이 이미 사용 중입니다.",
                "code": "DIMENSION_VALUE_CONFLICT",
                "violations": [{"field": "body.value", "message": "이미 사용 중인 값입니다."}],
            },
        },
        "multipleConflicts": {
            "summary": "대표 코드와 동등 용량 중복",
            "value": {
                "type": "/problems/dimension-multiple-conflicts",
                "title": "여러 Dimension 필드 충돌",
                "status": 409,
                "detail": "Memory 대표 코드와 동등한 용량이 모두 이미 사용 중입니다.",
                "code": "DIMENSION_MULTIPLE_CONFLICTS",
                "violations": [
                    {"field": "body.code", "message": "이미 사용 중인 값입니다."},
                    {"field": "body.value", "message": "이미 사용 중인 값입니다."},
                ],
            },
        },
    }
    return response


def _create_responses() -> dict[int | str, dict[str, Any]]:
    return {
        201: {"description": "Memory Dimension을 생성했습니다.", "headers": _etag_header()},
        401: INVALID_ACCESS_TOKEN_RESPONSE,
        403: AUTHORIZATION_DENIED_RESPONSE,
        409: _conflict_response(),
        422: _validation_response(
            description="Memory 생성 입력값이 유효하지 않습니다.", field="body.value"
        ),
        503: _service_unavailable_response(),
    }


def _list_responses() -> dict[int | str, dict[str, Any]]:
    return {
        200: {"description": "활성 Memory Dimension 목록을 반환합니다."},
        401: INVALID_ACCESS_TOKEN_RESPONSE,
        422: _validation_response(
            description="cursor 또는 limit이 유효하지 않습니다.", field="query.cursor"
        ),
        503: _service_unavailable_response(),
    }


def _get_responses() -> dict[int | str, dict[str, Any]]:
    return {
        200: {"description": "활성 Memory Dimension을 반환합니다.", "headers": _etag_header()},
        401: INVALID_ACCESS_TOKEN_RESPONSE,
        404: _problem_response(
            description="활성 Memory Dimension을 찾을 수 없습니다.",
            example={
                "type": "/problems/dimension-not-found",
                "title": "Dimension을 찾을 수 없음",
                "status": 404,
                "detail": "요청한 활성 Memory Dimension을 찾을 수 없습니다.",
                "code": "DIMENSION_NOT_FOUND",
            },
        ),
        422: _validation_response(
            description="Memory 식별자 형식이 유효하지 않습니다.", field="path.dimension_id"
        ),
        503: _service_unavailable_response(),
    }


def _update_responses() -> dict[int | str, dict[str, Any]]:
    conflict_response = _conflict_response()
    conflict_response["description"] = (
        "정규화된 Memory code·capacity_mb 또는 재합성된 MasterCode가 이미 사용 중입니다."
    )
    conflict_response["content"]["application/problem+json"]["examples"]["masterCodeConflict"] = {
        "summary": "MasterCode 재합성 충돌",
        "value": {
            "type": "/problems/master-code-conflict",
            "title": "MasterCode 충돌",
            "status": 409,
            "detail": "같은 참조 조합 또는 합성 코드의 MasterCode가 이미 존재합니다.",
            "code": "MASTER_CODE_CONFLICT",
        },
    }
    return {
        200: {
            "description": "현재 Memory Dimension 상태를 반환합니다.",
            "headers": {
                "ETag": {
                    "description": "응답 본문 version과 같은 현재 강한 ETag입니다.",
                    "schema": {"type": "string", "example": '"1"'},
                }
            },
        },
        401: INVALID_ACCESS_TOKEN_RESPONSE,
        403: AUTHORIZATION_DENIED_RESPONSE,
        404: _get_responses()[404],
        409: conflict_response,
        422: _validation_response(
            description="Memory 수정 입력값이 유효하지 않습니다.", field="body.value"
        ),
        503: _service_unavailable_response(),
        **dimension_precondition_responses(),
    }


def _etag_header() -> dict[str, Any]:
    return {
        "ETag": {
            "description": '정수 version N을 따옴표까지 포함한 강한 ETag "N"으로 표현합니다.',
            "schema": {"type": "string", "example": '"1"'},
        }
    }
