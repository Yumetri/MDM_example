"""HTTP contracts for Year and Network Dimensions."""

import base64
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Path, Query, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr

from mdm.api.auth import INVALID_ACCESS_TOKEN_RESPONSE
from mdm.api.authorization import AUTHORIZATION_DENIED_RESPONSE, build_authorization_guard
from mdm.api.preconditions import (
    DimensionIfMatchHeader,
    dimension_precondition_responses,
    parse_dimension_if_match_values,
)
from mdm.api.schemas import ProblemDetails
from mdm.application.auth import HumanPrincipal
from mdm.application.authorization import AuthorizationAction, AuthorizationPolicy
from mdm.application.numeric_dimensions import (
    CreateNetwork,
    CreateYear,
    GetNetwork,
    GetYear,
    ListNetworks,
    ListYears,
    NumericDimensionCursor,
    NumericDimensionPage,
    UpdateNetworkValue,
    UpdateYearValue,
)
from mdm.domain.dimensions import Dimension, DimensionValidationError

CURSOR_EXAMPLE = (
    "eyJpIjoiMDFhMDgzYzMtODhlOC03MTIzLTgwMDAtMDAwMDAwMDAwMDIxIiwidCI6"
    "IjIwMjYtMDktMTBUMDE6MjM6NDVaIiwidiI6MX0"
)
YEAR_EXAMPLE: dict[str, Any] = {
    "id": "01a083c3-88e8-7123-8000-000000000021",
    "code": "YR2026",
    "value": 2026,
    "version": 1,
    "created_at": "2026-09-10T01:23:45Z",
    "updated_at": "2026-09-10T01:23:45Z",
    "deleted_at": None,
}
NETWORK_EXAMPLE: dict[str, Any] = {
    **YEAR_EXAMPLE,
    "code": "NET5",
    "value": 5,
}


def _code_field(example: str) -> Any:
    return Field(
        min_length=1,
        max_length=32,
        description=(
            "MasterCode 합성에 사용할 대표 코드입니다. ASCII 소문자는 대문자로 바꾸지만 "
            "공백과 A-Z·0-9 이외 문자는 거부하며, 정규화 후 N으로만 이루어진 1~32자 값은 "
            "예약 코드라서 사용할 수 없습니다."
        ),
        examples=[example],
    )


def _reason_field(action: str = "생성") -> Any:
    return Field(
        description=(
            f"{action} 이유입니다. 앞뒤 일반 공백을 제거한 결과가 500자 이하여야 하며, "
            "빈 값은 저장하지 않습니다."
        ),
        examples=["값 수정" if action == "수정" else "신규 기준정보 등록"],
        json_schema_extra={"x-normalized-maxLength": 500},
    )


class _NumericDimensionCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: Annotated[StrictStr, _code_field("VALUE1")]
    value: StrictInt
    reason: Annotated[StrictStr | None, _reason_field()] = None


class YearCreateRequest(_NumericDimensionCreateRequest):
    """Year Dimension 생성 입력입니다."""

    code: Annotated[StrictStr, _code_field("yr2026")]
    value: Annotated[
        StrictInt,
        Field(
            ge=2000,
            le=2999,
            description="문자열·boolean·실수 변환을 허용하지 않는 2000~2999의 JSON 정수입니다.",
            examples=[2026],
        ),
    ]
    reason: Annotated[StrictStr | None, _reason_field()] = None


class NetworkCreateRequest(_NumericDimensionCreateRequest):
    """Network Dimension 생성 입력입니다."""

    code: Annotated[StrictStr, _code_field("net5")]
    value: Annotated[
        StrictInt,
        Field(
            ge=1,
            le=5,
            description=(
                "문자열·boolean·실수 변환을 허용하지 않는 1~5의 JSON 정수입니다. "
                "5G 같은 표시값은 저장하지 않습니다."
            ),
            examples=[5],
        ),
    ]
    reason: Annotated[StrictStr | None, _reason_field()] = None


class _NumericDimensionValueUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: StrictInt
    reason: Annotated[StrictStr | None, _reason_field("수정")] = None


class YearValueUpdateRequest(_NumericDimensionValueUpdateRequest):
    """Year Dimension value 수정 입력입니다."""

    model_config = ConfigDict(extra="forbid")
    value: Annotated[
        StrictInt,
        Field(
            ge=2000,
            le=2999,
            description="새 Year 값이며 문자열·boolean·실수 변환을 허용하지 않습니다.",
            examples=[2027],
        ),
    ]
    reason: Annotated[StrictStr | None, _reason_field("수정")] = None


class NetworkValueUpdateRequest(_NumericDimensionValueUpdateRequest):
    """Network Dimension value 수정 입력입니다."""

    model_config = ConfigDict(extra="forbid")
    value: Annotated[
        StrictInt,
        Field(
            ge=1,
            le=5,
            description="새 Network 세대 값이며 문자열·boolean·실수 변환을 허용하지 않습니다.",
            examples=[4],
        ),
    ]
    reason: Annotated[StrictStr | None, _reason_field("수정")] = None


class _NumericDimensionResponse(BaseModel):
    id: Annotated[UUID, Field(description="서버가 생성한 UUIDv7 식별자입니다.")]
    code: Annotated[str, Field(description="정규화된 대표 코드입니다.")]
    value: Annotated[int, Field(description="검증된 정수 Dimension 값입니다.")]
    version: Annotated[int, Field(description="현재 상태의 낙관적 잠금 버전입니다.")]
    created_at: Annotated[datetime, Field(description="생성 시각입니다.")]
    updated_at: Annotated[datetime, Field(description="현재 상태가 마지막으로 변경된 시각입니다.")]
    deleted_at: Annotated[
        None,
        Field(description="활성 Dimension만 반환하는 현재 엔드포인트에서는 항상 null입니다."),
    ]


class YearResponse(_NumericDimensionResponse):
    """현재 Year Dimension 상태입니다."""

    model_config = ConfigDict(json_schema_extra={"example": YEAR_EXAMPLE})
    value: Annotated[
        int,
        Field(ge=2000, le=2999, description="2000~2999 범위로 검증된 Year 값입니다."),
    ]


class NetworkResponse(_NumericDimensionResponse):
    """현재 Network Dimension 상태입니다."""

    model_config = ConfigDict(json_schema_extra={"example": NETWORK_EXAMPLE})
    value: Annotated[
        int,
        Field(ge=1, le=5, description="1~5 범위로 검증된 Network 세대 값입니다."),
    ]


class YearListResponse(BaseModel):
    """최신 생성 순서로 조회한 Year cursor 페이지입니다."""

    model_config = ConfigDict(
        json_schema_extra={"example": {"items": [YEAR_EXAMPLE], "next_cursor": CURSOR_EXAMPLE}}
    )
    items: Annotated[list[YearResponse], Field(description="현재 페이지의 활성 Year 목록입니다.")]
    next_cursor: Annotated[
        str | None,
        Field(
            max_length=512,
            description="다음 페이지가 있으면 전달되는 불투명 cursor이며 마지막이면 null입니다.",
            examples=[CURSOR_EXAMPLE],
        ),
    ]


class NetworkListResponse(BaseModel):
    """최신 생성 순서로 조회한 Network cursor 페이지입니다."""

    model_config = ConfigDict(
        json_schema_extra={"example": {"items": [NETWORK_EXAMPLE], "next_cursor": CURSOR_EXAMPLE}}
    )
    items: Annotated[
        list[NetworkResponse], Field(description="현재 페이지의 활성 Network 목록입니다.")
    ]
    next_cursor: Annotated[
        str | None,
        Field(
            max_length=512,
            description="다음 페이지가 있으면 전달되는 불투명 cursor이며 마지막이면 null입니다.",
            examples=[CURSOR_EXAMPLE],
        ),
    ]


@dataclass(frozen=True, slots=True)
class _RouterConfig:
    singular: str
    display_name: str
    collection: str
    tag: str
    request_model: type[_NumericDimensionCreateRequest]
    update_request_model: type[_NumericDimensionValueUpdateRequest]
    response_model: type[_NumericDimensionResponse]
    list_model: type[BaseModel]


def build_year_router(
    *,
    create_dimension: CreateYear,
    get_dimension: GetYear,
    list_dimensions: ListYears,
    update_dimension: UpdateYearValue,
    principal_dependency: Callable[..., HumanPrincipal],
    authorization: AuthorizationPolicy,
) -> APIRouter:
    return _build_router(
        _RouterConfig(
            "year",
            "Year",
            "years",
            "Year Dimensions",
            YearCreateRequest,
            YearValueUpdateRequest,
            YearResponse,
            YearListResponse,
        ),
        create_dimension=create_dimension,
        get_dimension=get_dimension,
        list_dimensions=list_dimensions,
        update_dimension=update_dimension,
        principal_dependency=principal_dependency,
        authorization=authorization,
    )


def build_network_router(
    *,
    create_dimension: CreateNetwork,
    get_dimension: GetNetwork,
    list_dimensions: ListNetworks,
    update_dimension: UpdateNetworkValue,
    principal_dependency: Callable[..., HumanPrincipal],
    authorization: AuthorizationPolicy,
) -> APIRouter:
    return _build_router(
        _RouterConfig(
            "network",
            "Network",
            "networks",
            "Network Dimensions",
            NetworkCreateRequest,
            NetworkValueUpdateRequest,
            NetworkResponse,
            NetworkListResponse,
        ),
        create_dimension=create_dimension,
        get_dimension=get_dimension,
        list_dimensions=list_dimensions,
        update_dimension=update_dimension,
        principal_dependency=principal_dependency,
        authorization=authorization,
    )


def _build_router(
    config: _RouterConfig,
    *,
    create_dimension: Any,
    get_dimension: Any,
    list_dimensions: Any,
    update_dimension: Any,
    principal_dependency: Callable[..., HumanPrincipal],
    authorization: AuthorizationPolicy,
) -> APIRouter:
    router = APIRouter(prefix=f"/dimensions/{config.collection}", tags=[config.tag])
    mutation_guard = build_authorization_guard(
        principal_dependency, authorization, AuthorizationAction.MUTATE_DATA
    )
    read_guard = build_authorization_guard(
        principal_dependency, authorization, AuthorizationAction.READ_DATA
    )

    async def create_route(
        payload: _NumericDimensionCreateRequest,
        response: Response,
        principal: Annotated[HumanPrincipal, Depends(mutation_guard)],
    ) -> _NumericDimensionResponse:
        try:
            dimension = await create_dimension.execute(
                principal,
                code=payload.code,
                value=payload.value,
                reason=payload.reason,
            )
        except DimensionValidationError as error:
            raise DimensionValidationError(f"body.{error.field}", error.message) from error
        response.headers["ETag"] = _etag(dimension.version)
        return _response(dimension, config.response_model)

    create_route.__name__ = f"create_{config.singular}_dimension"
    create_route.__annotations__["payload"] = config.request_model
    create_route.__annotations__["return"] = config.response_model
    router.add_api_route(
        "",
        create_route,
        methods=["POST"],
        operation_id=f"create_{config.singular}_dimension",
        response_model=config.response_model,
        status_code=status.HTTP_201_CREATED,
        summary=f"{config.display_name} Dimension 생성",
        description=(
            f"ADMIN 또는 SUPER_ADMIN이 {config.display_name} 대표 코드와 엄격한 정수 값을 "
            "생성합니다. 인증 주체와 역할은 Bearer JWT에서만 가져옵니다."
        ),
        responses=_create_responses(config.display_name),
    )

    async def list_route(
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
    ) -> BaseModel:
        after = None if cursor is None else _decode_cursor(cursor)
        page = await list_dimensions.execute(principal, after=after, limit=limit)
        return _page_response(page, config)

    list_route.__name__ = f"list_{config.singular}_dimensions"
    list_route.__annotations__["return"] = config.list_model
    router.add_api_route(
        "",
        list_route,
        methods=["GET"],
        operation_id=f"list_{config.singular}_dimensions",
        response_model=config.list_model,
        status_code=status.HTTP_200_OK,
        summary=f"{config.display_name} Dimension 목록 조회",
        description=(
            f"활성 {config.display_name}를 생성 시각과 ID의 내림차순으로 조회합니다. "
            "next_cursor를 다음 요청에 그대로 전달하며 전체 항목 수는 제공하지 않습니다. "
            "페이지마다 limit을 바꿀 수 있고 cursor는 만료되지 않습니다. 페이지를 순회하는 동안 "
            "새로 생긴 항목은 이미 지난 앞쪽 구간에 위치하므로 뒤 페이지에 끼어들지 않으며, "
            "각 페이지 요청 시점에 이미 삭제된 항목은 결과에서 제외됩니다."
        ),
        responses=_list_responses(config.display_name),
    )

    async def get_route(
        dimension_id: Annotated[
            UUID,
            Path(description=f"조회할 {config.display_name} Dimension의 UUID 식별자입니다."),
        ],
        response: Response,
        principal: Annotated[HumanPrincipal, Depends(read_guard)],
    ) -> _NumericDimensionResponse:
        dimension = await get_dimension.execute(principal, dimension_id)
        response.headers["ETag"] = _etag(dimension.version)
        return _response(dimension, config.response_model)

    get_route.__name__ = f"get_{config.singular}_dimension"
    get_route.__annotations__["return"] = config.response_model
    router.add_api_route(
        "/{dimension_id}",
        get_route,
        methods=["GET"],
        operation_id=f"get_{config.singular}_dimension",
        response_model=config.response_model,
        status_code=status.HTTP_200_OK,
        summary=f"{config.display_name} Dimension 단건 조회",
        description=(
            f"UUID로 활성 {config.display_name}를 조회하고 본문의 version과 같은 강한 ETag를 "
            "반환합니다."
        ),
        responses=_get_responses(config.display_name),
    )

    async def update_route(
        dimension_id: Annotated[
            UUID,
            Path(description=f"수정할 {config.display_name} Dimension의 UUID 식별자입니다."),
        ],
        payload: _NumericDimensionValueUpdateRequest,
        request: Request,
        response: Response,
        principal: Annotated[HumanPrincipal, Depends(mutation_guard)],
        if_match: DimensionIfMatchHeader,
    ) -> _NumericDimensionResponse:
        expected_version = parse_dimension_if_match_values(request.headers.getlist("if-match"))
        try:
            dimension = await update_dimension.execute(
                principal,
                dimension_id,
                expected_version=expected_version,
                value=payload.value,
                reason=payload.reason,
            )
        except DimensionValidationError as error:
            raise DimensionValidationError(f"body.{error.field}", error.message) from error
        response.headers["ETag"] = _etag(dimension.version)
        return _response(dimension, config.response_model)

    update_route.__name__ = f"update_{config.singular}_dimension_value"
    update_route.__annotations__["payload"] = config.update_request_model
    update_route.__annotations__["return"] = config.response_model
    router.add_api_route(
        "/{dimension_id}",
        update_route,
        methods=["PATCH"],
        operation_id=f"update_{config.singular}_dimension_value",
        response_model=config.response_model,
        status_code=status.HTTP_200_OK,
        summary=f"{config.display_name} Dimension 값 수정",
        description=(
            f"ADMIN 또는 SUPER_ADMIN이 강한 If-Match로 {config.display_name} value만 조건부로 "
            "수정합니다. code 입력은 허용하지 않으며, 같은 값이면 version·시각·감사 로그를 "
            "변경하지 않습니다."
        ),
        responses=_update_responses(config.display_name),
    )
    return router


def _response(
    dimension: Dimension[Any], response_model: type[_NumericDimensionResponse]
) -> _NumericDimensionResponse:
    if dimension.deleted_at is not None:
        raise ValueError("inactive numeric Dimension cannot be serialized")
    return response_model(
        id=dimension.id,
        code=dimension.code.value,
        value=dimension.value.value,
        version=dimension.version,
        created_at=dimension.created_at,
        updated_at=dimension.updated_at,
        deleted_at=dimension.deleted_at,
    )


def _page_response(page: NumericDimensionPage[Any], config: _RouterConfig) -> BaseModel:
    next_cursor = None
    if page.has_more and page.items:
        last = page.items[-1]
        next_cursor = _encode_cursor(NumericDimensionCursor(created_at=last.created_at, id=last.id))
    return config.list_model(
        items=[_response(item, config.response_model) for item in page.items],
        next_cursor=next_cursor,
    )


def _etag(version: int) -> str:
    return f'"{version}"'


def _encode_cursor(cursor: NumericDimensionCursor) -> str:
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


def _decode_cursor(encoded: str) -> NumericDimensionCursor:
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
        return NumericDimensionCursor(created_at=timestamp, id=UUID(payload["i"]))
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


def _service_unavailable_response(name: str) -> dict[str, Any]:
    return _problem_response(
        description=f"{name} 데이터를 일시적으로 처리할 수 없습니다.",
        example={
            "type": "/problems/service-unavailable",
            "title": "서비스를 사용할 수 없음",
            "status": 503,
            "detail": "요청을 일시적으로 처리할 수 없습니다. 잠시 후 다시 시도해 주세요.",
            "code": "SERVICE_UNAVAILABLE",
        },
    )


def _conflict_response(name: str) -> dict[str, Any]:
    response = _problem_response(
        description=f"정규화된 {name} code 또는 value가 이미 사용 중입니다.",
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
                "detail": f"정규화된 {name} 대표 코드가 이미 사용 중입니다.",
                "code": "DIMENSION_CODE_CONFLICT",
                "violations": [{"field": "body.code", "message": "이미 사용 중인 값입니다."}],
            },
        },
        "valueConflict": {
            "summary": "값 중복",
            "value": {
                "type": "/problems/dimension-value-conflict",
                "title": "Dimension 값 충돌",
                "status": 409,
                "detail": f"{name} 값이 이미 사용 중입니다.",
                "code": "DIMENSION_VALUE_CONFLICT",
                "violations": [{"field": "body.value", "message": "이미 사용 중인 값입니다."}],
            },
        },
        "multipleConflicts": {
            "summary": "대표 코드와 값 중복",
            "value": {
                "type": "/problems/dimension-multiple-conflicts",
                "title": "여러 Dimension 필드 충돌",
                "status": 409,
                "detail": f"{name} 대표 코드와 값이 모두 이미 사용 중입니다.",
                "code": "DIMENSION_MULTIPLE_CONFLICTS",
                "violations": [
                    {"field": "body.code", "message": "이미 사용 중인 값입니다."},
                    {"field": "body.value", "message": "이미 사용 중인 값입니다."},
                ],
            },
        },
    }
    return response


def _create_responses(name: str) -> dict[int | str, dict[str, Any]]:
    return {
        201: {"description": f"{name} Dimension을 생성했습니다.", "headers": _etag_header()},
        401: INVALID_ACCESS_TOKEN_RESPONSE,
        403: AUTHORIZATION_DENIED_RESPONSE,
        409: _conflict_response(name),
        422: _validation_response(
            description=f"{name} 생성 입력값이 유효하지 않습니다.", field="body.value"
        ),
        503: _service_unavailable_response(name),
    }


def _list_responses(name: str) -> dict[int | str, dict[str, Any]]:
    return {
        200: {"description": f"활성 {name} Dimension 목록을 반환합니다."},
        401: INVALID_ACCESS_TOKEN_RESPONSE,
        422: _validation_response(
            description="cursor 또는 limit가 유효하지 않습니다.", field="query.cursor"
        ),
        503: _service_unavailable_response(name),
    }


def _get_responses(name: str) -> dict[int | str, dict[str, Any]]:
    return {
        200: {"description": f"활성 {name} Dimension을 반환합니다.", "headers": _etag_header()},
        401: INVALID_ACCESS_TOKEN_RESPONSE,
        404: _problem_response(
            description=f"활성 {name} Dimension을 찾을 수 없습니다.",
            example={
                "type": "/problems/dimension-not-found",
                "title": "Dimension을 찾을 수 없음",
                "status": 404,
                "detail": f"요청한 활성 {name} Dimension을 찾을 수 없습니다.",
                "code": "DIMENSION_NOT_FOUND",
            },
        ),
        422: _validation_response(
            description=f"{name} 식별자 형식이 유효하지 않습니다.", field="path.dimension_id"
        ),
        503: _service_unavailable_response(name),
    }


def _update_responses(name: str) -> dict[int | str, dict[str, Any]]:
    return {
        200: {
            "description": f"현재 {name} Dimension 상태를 반환합니다.",
            "headers": {
                "ETag": {
                    "description": "응답 본문 version과 같은 현재 강한 ETag입니다.",
                    "schema": {"type": "string", "example": '"1"'},
                }
            },
        },
        401: INVALID_ACCESS_TOKEN_RESPONSE,
        403: AUTHORIZATION_DENIED_RESPONSE,
        404: _get_responses(name)[404],
        409: _problem_response(
            description=f"{name} value가 이미 사용 중입니다.",
            example={
                "type": "/problems/dimension-value-conflict",
                "title": "Dimension 값 충돌",
                "status": 409,
                "detail": f"{name} 값이 이미 사용 중입니다.",
                "code": "DIMENSION_VALUE_CONFLICT",
                "violations": [{"field": "body.value", "message": "이미 사용 중인 값입니다."}],
            },
        ),
        422: _validation_response(
            description=f"{name} 수정 입력값이 유효하지 않습니다.", field="body.value"
        ),
        503: _service_unavailable_response(name),
        **dimension_precondition_responses(),
    }


def _etag_header() -> dict[str, Any]:
    return {
        "ETag": {
            "description": '정수 version N을 따옴표까지 포함한 강한 ETag "N"으로 표현합니다.',
            "schema": {"type": "string", "example": '"1"'},
        }
    }
