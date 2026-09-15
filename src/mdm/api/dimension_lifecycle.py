"""Typed Dimension deletion, tombstone and restoration HTTP contracts."""

from collections.abc import Callable
from copy import deepcopy
from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Path, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StrictStr

from mdm.api.auth import INVALID_ACCESS_TOKEN_RESPONSE
from mdm.api.authorization import AUTHORIZATION_DENIED_RESPONSE, build_authorization_guard
from mdm.api.dimensions import COMPANY_RESPONSE_EXAMPLE, CompanyResponse
from mdm.api.memory_dimensions import MEMORY_EXAMPLE, MemoryResponse, MemoryValueResponse
from mdm.api.numeric_dimensions import NETWORK_EXAMPLE, YEAR_EXAMPLE, NetworkResponse, YearResponse
from mdm.api.preconditions import (
    DimensionIfMatchHeader,
    dimension_precondition_responses,
    parse_dimension_if_match_values,
)
from mdm.api.routes import API_V1_PREFIX
from mdm.api.schemas import ProblemDetails
from mdm.api.string_dimensions import (
    BRAND_EXAMPLE,
    CATEGORY_EXAMPLE,
    COUNTRY_EXAMPLE,
    MODEL_EXAMPLE,
    BrandResponse,
    CategoryResponse,
    CountryResponse,
    ModelResponse,
)
from mdm.application.auth import HumanPrincipal
from mdm.application.authorization import AuthorizationAction, AuthorizationPolicy
from mdm.application.dimension_lifecycle import (
    DeleteDimension,
    GetDimensionTombstone,
    RestoreDimension,
)
from mdm.domain.dimensions import Dimension, DimensionValidationError, MemoryValue


class DimensionLifecycleRequest(BaseModel):
    """삭제·복원 사유입니다. 사유가 없으면 빈 JSON 객체를 전송합니다."""

    model_config = ConfigDict(extra="forbid")
    reason: Annotated[
        StrictStr | None,
        Field(
            description="선택적 사유입니다. 앞뒤 일반 공백 제거 후 500자 이하여야 합니다. "
            "생략·null·빈 문자열은 사유 없음이며, 탭·줄바꿈·제어문자는 허용하지 않습니다.",
            examples=["사용 종료"],
            json_schema_extra={"x-normalized-maxLength": 500},
        ),
    ] = None


class _DeletedDimensionResponse(BaseModel):
    id: Annotated[UUID, Field(description="Dimension의 UUIDv7 식별자입니다.")]
    code: Annotated[str, Field(pattern=r"^[A-Z0-9]{1,32}$", description="삭제 전 대표 코드입니다.")]
    version: Annotated[int, Field(ge=1, le=2_147_483_647, description="현재 상태의 버전입니다.")]
    created_at: Annotated[datetime, Field(description="최초 생성 시각입니다.")]
    updated_at: Annotated[datetime, Field(description="현재 상태가 마지막으로 변경된 시각입니다.")]
    deleted_at: Annotated[
        datetime, Field(description="논리 삭제 시각입니다. 항상 값이 존재합니다.")
    ]


def _deleted_example(original: dict[str, Any]) -> dict[str, Any]:
    return {
        **original,
        "version": 2,
        "updated_at": "2026-09-15T01:00:00Z",
        "deleted_at": "2026-09-15T01:00:00Z",
    }


class CompanyTombstoneResponse(_DeletedDimensionResponse):
    """삭제 상태인 Company Dimension입니다."""

    model_config = ConfigDict(
        json_schema_extra={"example": _deleted_example(COMPANY_RESPONSE_EXAMPLE)}
    )
    value: Annotated[
        str,
        Field(
            min_length=1,
            max_length=128,
            pattern=r"^[A-Z0-9]+(?:_[A-Z0-9]+)*$",
            description="삭제 전 Company 값입니다.",
        ),
    ]


class BrandTombstoneResponse(_DeletedDimensionResponse):
    """삭제 상태인 Brand Dimension입니다."""

    model_config = ConfigDict(json_schema_extra={"example": _deleted_example(BRAND_EXAMPLE)})
    value: Annotated[
        str,
        Field(
            min_length=1,
            max_length=128,
            pattern=r"^[A-Z0-9]+(?:_[A-Z0-9]+)*$",
            description="삭제 전 Brand 값입니다.",
        ),
    ]


class ModelTombstoneResponse(_DeletedDimensionResponse):
    """삭제 상태인 Model Dimension입니다."""

    model_config = ConfigDict(json_schema_extra={"example": _deleted_example(MODEL_EXAMPLE)})
    value: Annotated[
        str,
        Field(
            min_length=1,
            max_length=128,
            pattern=r"^[A-Z0-9]+(?:_[A-Z0-9]+)*$",
            description="삭제 전 Model 값입니다.",
        ),
    ]


class CategoryTombstoneResponse(_DeletedDimensionResponse):
    """삭제 상태인 Category Dimension입니다."""

    model_config = ConfigDict(json_schema_extra={"example": _deleted_example(CATEGORY_EXAMPLE)})
    value: Annotated[
        str,
        Field(
            min_length=1,
            max_length=128,
            pattern=r"^[A-Z0-9]+(?:_[A-Z0-9]+)*$",
            description="삭제 전 Category 값입니다.",
        ),
    ]


class CountryTombstoneResponse(_DeletedDimensionResponse):
    """삭제 상태인 Country Dimension입니다."""

    model_config = ConfigDict(json_schema_extra={"example": _deleted_example(COUNTRY_EXAMPLE)})
    value: Annotated[
        str,
        Field(
            min_length=1,
            max_length=128,
            pattern=r"^[A-Z0-9]+(?:_[A-Z0-9]+)*$",
            description="삭제 전 Country 값입니다.",
        ),
    ]


class YearTombstoneResponse(_DeletedDimensionResponse):
    """삭제 상태인 Year Dimension입니다."""

    model_config = ConfigDict(json_schema_extra={"example": _deleted_example(YEAR_EXAMPLE)})
    value: Annotated[int, Field(ge=2000, le=2999, description="삭제 전 Year 값입니다.")]


class NetworkTombstoneResponse(_DeletedDimensionResponse):
    """삭제 상태인 Network Dimension입니다."""

    model_config = ConfigDict(json_schema_extra={"example": _deleted_example(NETWORK_EXAMPLE)})
    value: Annotated[int, Field(ge=1, le=5, description="삭제 전 Network 값입니다.")]


class MemoryTombstoneResponse(_DeletedDimensionResponse):
    """삭제 상태인 Memory Dimension입니다."""

    model_config = ConfigDict(json_schema_extra={"example": _deleted_example(MEMORY_EXAMPLE)})
    value: Annotated[MemoryValueResponse, Field(description="삭제 전 Memory 값입니다.")]


_LIFECYCLE_MODELS: dict[str, tuple[str, str, type[BaseModel], type[BaseModel]]] = {
    "company": ("companies", "Company", CompanyResponse, CompanyTombstoneResponse),
    "brand": ("brands", "Brand", BrandResponse, BrandTombstoneResponse),
    "model": ("models", "Model", ModelResponse, ModelTombstoneResponse),
    "category": ("categories", "Category", CategoryResponse, CategoryTombstoneResponse),
    "country": ("countries", "Country", CountryResponse, CountryTombstoneResponse),
    "year": ("years", "Year", YearResponse, YearTombstoneResponse),
    "network": ("networks", "Network", NetworkResponse, NetworkTombstoneResponse),
    "memory": ("memories", "Memory", MemoryResponse, MemoryTombstoneResponse),
}


def _problem(status: int, code: str, title: str, detail: str) -> dict[str, Any]:
    return {
        "model": ProblemDetails,
        "description": detail,
        "content": {
            "application/problem+json": {
                "schema": {"$ref": "#/components/schemas/ProblemDetails"},
                "example": {
                    "type": "/problems/" + code.lower().replace("_", "-"),
                    "title": title,
                    "status": status,
                    "detail": detail,
                    "code": code,
                },
            },
        },
    }


NOT_DELETED_RESPONSE = _problem(
    409,
    "DIMENSION_NOT_DELETED",
    "삭제되지 않은 Dimension",
    "활성 Dimension은 삭제 상태 조회 또는 복원의 대상이 아닙니다.",
)
IN_USE_RESPONSE = _problem(
    409,
    "DIMENSION_IN_USE",
    "사용 중인 Dimension",
    "활성 MasterCode가 참조하는 Dimension은 삭제할 수 없습니다.",
)


def build_dimension_lifecycle_router[ValueT](
    *,
    singular: str,
    delete: DeleteDimension[ValueT],
    get_tombstone: GetDimensionTombstone[ValueT],
    restore: RestoreDimension[ValueT],
    principal_dependency: Callable[..., HumanPrincipal],
    authorization: AuthorizationPolicy,
) -> APIRouter:
    collection, display, active_model, tombstone_model = _LIFECYCLE_MODELS[singular]
    router = APIRouter(
        prefix=f"{API_V1_PREFIX}/dimensions/{collection}", tags=[f"{display} Dimensions"]
    )
    mutation_guard = build_authorization_guard(
        principal_dependency, authorization, AuthorizationAction.MUTATE_DATA
    )
    tombstone_guard = build_authorization_guard(
        principal_dependency, authorization, AuthorizationAction.READ_TOMBSTONE
    )
    common = {
        401: INVALID_ACCESS_TOKEN_RESPONSE,
        403: AUTHORIZATION_DENIED_RESPONSE,
        404: _problem(
            404,
            "DIMENSION_NOT_FOUND",
            "Dimension을 찾을 수 없음",
            "요청한 경로에서 조회할 수 있는 Dimension이 없습니다.",
        ),
        422: _problem(
            422,
            "VALIDATION_ERROR",
            "유효하지 않은 요청",
            "경로의 UUID 또는 JSON 객체 본문이 유효하지 않습니다.",
        ),
        503: _problem(
            503,
            "SERVICE_UNAVAILABLE",
            "서비스를 사용할 수 없음",
            "일시적인 오류로 요청을 처리할 수 없습니다. 잠시 후 다시 시도해 주세요.",
        ),
    }
    etag_response: dict[str, Any] = {
        "description": "현재 상태와 같은 version의 강한 ETag를 반환합니다.",
        "headers": {
            "ETag": {
                "description": "따옴표로 감싼 현재 version입니다.",
                "schema": {"type": "string", "pattern": r'^"[1-9][0-9]*"$'},
                "example": '"2"',
            }
        },
    }

    restored_response = deepcopy(etag_response)
    restored_response["headers"]["ETag"]["example"] = '"3"'
    restored_response["content"] = {
        "application/json": {
            "example": {
                **active_model.model_json_schema()["example"],
                "version": 3,
                "updated_at": "2026-09-15T01:01:00Z",
                "deleted_at": None,
            }
        }
    }

    @router.post(
        "/{dimension_id}/delete",
        operation_id=f"delete_{singular}_dimension",
        response_model=tombstone_model,
        status_code=200,
        summary=f"{display} 논리 삭제",
        description="ADMIN·SUPER_ADMIN이 활성 Dimension을 논리 삭제합니다. "
        "활성 MasterCode 참조가 있으면 거부하며, 삭제된 MasterCode의 참조는 보존합니다. "
        "직전 활성 상태 ETag를 If-Match로 보내고 사유가 없으면 {}를 전송합니다. "
        "성공 시 version이 한 번 증가하며, 반환한 상태와 ETag로 복원을 요청할 수 있습니다. "
        "이미 삭제된 행은 404입니다.",
        responses={
            **common,
            **dimension_precondition_responses(),
            200: etag_response,
            409: IN_USE_RESPONSE,
        },
    )
    async def delete_dimension(
        dimension_id: Annotated[UUID, Path(description="삭제할 Dimension UUID입니다.")],
        payload: DimensionLifecycleRequest,
        request: Request,
        response: Response,
        principal: Annotated[HumanPrincipal, Depends(mutation_guard)],
        if_match: DimensionIfMatchHeader,
    ) -> BaseModel:
        version = parse_dimension_if_match_values(request.headers.getlist("if-match"))
        try:
            result = await delete.execute(
                principal, dimension_id, expected_version=version, reason=payload.reason
            )
        except DimensionValidationError as error:
            raise DimensionValidationError(f"body.{error.field}", error.message) from error
        response.headers["ETag"] = f'"{result.version}"'
        return _serialize(result, tombstone_model)

    @router.get(
        "/{dimension_id}/tombstone",
        operation_id=f"get_{singular}_dimension_tombstone",
        response_model=tombstone_model,
        status_code=200,
        summary=f"삭제된 {display} 단건 조회",
        description="ADMIN·SUPER_ADMIN이 삭제 상태와 현재 ETag를 조회합니다. "
        "상태를 변경하지 않으며 활성 행은 409, 없는 행은 404입니다. "
        "삭제 응답을 잃었을 때 이 경로에서 복원에 필요한 ETag를 다시 얻을 수 있습니다.",
        responses={
            **common,
            200: etag_response,
            409: NOT_DELETED_RESPONSE,
            422: _problem(
                422,
                "VALIDATION_ERROR",
                "유효하지 않은 요청",
                "경로의 Dimension UUID가 유효하지 않습니다.",
            ),
        },
    )
    async def get_deleted_dimension(
        dimension_id: Annotated[UUID, Path(description="삭제 상태를 조회할 Dimension UUID입니다.")],
        response: Response,
        principal: Annotated[HumanPrincipal, Depends(tombstone_guard)],
    ) -> BaseModel:
        result = await get_tombstone.execute(principal, dimension_id)
        response.headers["ETag"] = f'"{result.version}"'
        return _serialize(result, tombstone_model)

    @router.post(
        "/{dimension_id}/restore",
        operation_id=f"restore_{singular}_dimension",
        response_model=active_model,
        status_code=200,
        summary=f"{display} 복원",
        description="ADMIN·SUPER_ADMIN이 삭제된 Dimension을 기존 UUID·code·value 그대로 "
        "복원합니다. 삭제 응답 또는 삭제 상태 조회에서 받은 ETag를 If-Match로 보내고, "
        "사유가 없으면 {}를 전송합니다. 성공 시 version이 한 번 증가하고 "
        "deleted_at이 null인 활성 상태와 새 ETag를 반환합니다. 이미 활성인 행은 409입니다.",
        responses={
            **common,
            **dimension_precondition_responses(),
            200: restored_response,
            409: NOT_DELETED_RESPONSE,
        },
    )
    async def restore_dimension(
        dimension_id: Annotated[UUID, Path(description="복원할 Dimension UUID입니다.")],
        payload: DimensionLifecycleRequest,
        request: Request,
        response: Response,
        principal: Annotated[HumanPrincipal, Depends(mutation_guard)],
        if_match: DimensionIfMatchHeader,
    ) -> BaseModel:
        version = parse_dimension_if_match_values(request.headers.getlist("if-match"))
        try:
            result = await restore.execute(
                principal, dimension_id, expected_version=version, reason=payload.reason
            )
        except DimensionValidationError as error:
            raise DimensionValidationError(f"body.{error.field}", error.message) from error
        response.headers["ETag"] = f'"{result.version}"'
        return _serialize(result, active_model)

    return router


def _serialize(dimension: Dimension[Any], model: type[BaseModel]) -> BaseModel:
    value = dimension.value
    serialized_value = (
        {"amount": value.amount, "unit": value.unit.value, "capacity_mb": value.capacity_mb}
        if isinstance(value, MemoryValue)
        else value.value
    )
    return model.model_validate(
        {
            "id": dimension.id,
            "code": dimension.code.value,
            "value": serialized_value,
            "version": dimension.version,
            "created_at": dimension.created_at,
            "updated_at": dimension.updated_at,
            "deleted_at": dimension.deleted_at,
        }
    )
