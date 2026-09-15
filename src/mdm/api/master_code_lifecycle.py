"""Administrator HTTP contracts for MasterCode deletion and restoration."""

from collections.abc import Callable
from copy import deepcopy
from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Path, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StrictStr

from mdm.api.auth import INVALID_ACCESS_TOKEN_RESPONSE
from mdm.api.authorization import AUTHORIZATION_DENIED_RESPONSE, build_authorization_guard
from mdm.api.master_codes import (
    MASTER_CODE_RESPONSE_EXAMPLE,
    MasterCodeDimensionsResponse,
    MasterCodeIfMatchHeader,
    MasterCodeResponse,
    _dimension_response,
    _etag_response,
    _master_code_precondition_response,
    _parse_master_code_if_match_values,
    _problem_response,
    _response,
    _service_unavailable_response,
    _validation_response,
)
from mdm.api.routes import API_V1_PREFIX
from mdm.application.auth import HumanPrincipal
from mdm.application.authorization import AuthorizationAction, AuthorizationPolicy
from mdm.application.master_code_lifecycle import (
    DeleteMasterCode,
    GetMasterCodeTombstone,
    RestoreMasterCode,
)
from mdm.domain.dimensions import DimensionValidationError
from mdm.domain.master_codes import MasterCode, MasterCodeDimensions

TOMBSTONE_EXAMPLE = {
    **MASTER_CODE_RESPONSE_EXAMPLE,
    "version": 2,
    "updated_at": "2026-09-15T01:23:45Z",
    "deleted_at": "2026-09-15T01:23:45Z",
}

RESTORED_EXAMPLE = {
    **MASTER_CODE_RESPONSE_EXAMPLE,
    "version": 3,
    "updated_at": "2026-09-15T01:24:45Z",
}
# Example references retain their initial Dimension versions.
TOMBSTONE_ETAG_EXAMPLE = '"mc-1-90729582ebb25a0e8673f52504afa7293e7228cc467b3ea76180b843854ea957"'
RESTORED_ETAG_EXAMPLE = '"mc-1-b1208392fa4f32258ab31adc5ba0cdba7cfe5445b5c364b6d67550438f76f5a8"'


class MasterCodeLifecycleRequest(BaseModel):
    """삭제 또는 복원 사유입니다. 사유가 없어도 빈 객체를 전송합니다."""

    model_config = ConfigDict(
        extra="forbid", json_schema_extra={"example": {"reason": "상태 정정"}}
    )
    reason: Annotated[
        StrictStr | None,
        Field(
            description=(
                "삭제 또는 복원 사유입니다. 생략 또는 null은 사유 없음입니다. 앞뒤 일반 "
                "공백을 제거하며, 빈 문자열은 저장하지 않습니다. 정규화 후 500자 이하여야 "
                "하고 탭·줄바꿈·제어문자는 허용하지 않습니다."
            ),
            json_schema_extra={"x-normalized-maxLength": 500},
        ),
    ] = None


class MasterCodeTombstoneResponse(BaseModel):
    """삭제된 MasterCode와 보존된 참조의 현재 값입니다."""

    model_config = ConfigDict(json_schema_extra={"example": TOMBSTONE_EXAMPLE})
    id: Annotated[UUID, Field(description="MasterCode UUIDv7 식별자입니다.")]
    code: Annotated[
        str,
        Field(
            description=(
                "삭제 시 보존한 합성 코드입니다. 삭제 상태에서도 참조 Dimension의 "
                "대표 코드가 변경되면 함께 재합성되어 현재 코드를 반영합니다."
            )
        ),
    ]
    version: Annotated[int, Field(description="MasterCode 자체 상태의 낙관적 잠금 버전입니다.")]
    created_at: Annotated[datetime, Field(description="MasterCode 생성 시각입니다.")]
    updated_at: Annotated[
        datetime, Field(description="MasterCode 자체 상태의 마지막 변경 시각입니다.")
    ]
    deleted_at: Annotated[datetime, Field(description="MasterCode가 논리 삭제된 시각입니다.")]
    dimensions: Annotated[
        MasterCodeDimensionsResponse,
        Field(
            description=(
                "삭제된 Dimension을 포함한 보존된 참조의 현재 값입니다. 해당 없음은 null입니다."
            )
        ),
    ]


def _conflict(*, inactive: bool = False) -> dict[str, Any]:
    examples: dict[str, dict[str, Any]] = {
        "notDeleted": {
            "summary": "이미 활성인 MasterCode",
            "type": "/problems/master-code-not-deleted",
            "title": "삭제되지 않은 MasterCode",
            "status": 409,
            "detail": "요청한 MasterCode가 삭제 상태가 아닙니다.",
            "code": "MASTER_CODE_NOT_DELETED",
        }
    }
    if inactive:
        examples["inactiveReference"] = {
            "summary": "삭제된 Dimension 참조",
            "type": "/problems/master-code-reference-inactive",
            "title": "복원할 수 없는 MasterCode 참조",
            "status": 409,
            "detail": "삭제된 Dimension 참조가 있어 MasterCode를 복원할 수 없습니다.",
            "code": "MASTER_CODE_REFERENCE_INACTIVE",
            "violations": [{"field": "company_id", "message": "삭제된 Dimension 참조입니다."}],
        }
    return _problem_response(
        description="현재 상태에서 요청을 처리할 수 없습니다.", examples=examples
    )


def _responses(action: str) -> dict[int | str, dict[str, Any]]:
    success = deepcopy(_etag_response("요청 처리 후 MasterCode 상태와 ETag를 반환합니다."))
    if action != "restore":
        success["content"]["application/json"].update(
            {
                "schema": {"$ref": "#/components/schemas/MasterCodeTombstoneResponse"},
                "example": deepcopy(TOMBSTONE_EXAMPLE),
            }
        )
    else:
        success["content"]["application/json"]["example"] = deepcopy(RESTORED_EXAMPLE)
    success["headers"]["ETag"]["schema"]["example"] = (
        RESTORED_ETAG_EXAMPLE if action == "restore" else TOMBSTONE_ETAG_EXAMPLE
    )
    success["headers"]["ETag"]["description"] += (
        " mc- 뒤 숫자는 알고리즘 버전이며 MasterCode의 version 필드와는 다릅니다."
    )
    result: dict[int | str, dict[str, Any]] = {
        200: success,
        401: deepcopy(INVALID_ACCESS_TOKEN_RESPONSE),
        403: deepcopy(AUTHORIZATION_DENIED_RESPONSE),
        404: _problem_response(
            description="대상 MasterCode를 찾을 수 없습니다.",
            example={
                "type": "/problems/master-code-not-found",
                "title": "MasterCode를 찾을 수 없음",
                "status": 404,
                "detail": "요청한 MasterCode를 찾을 수 없습니다.",
                "code": "MASTER_CODE_NOT_FOUND",
            },
        ),
        422: _validation_response(),
        503: _service_unavailable_response(),
    }
    if action != "delete":
        result[409] = _conflict(inactive=action == "restore")
    if action != "tombstone":
        for status in (400, 412, 428):
            result[status] = _master_code_precondition_response(status)
        result[428]["description"] = "조건부 삭제·복원에 필요한 If-Match 헤더가 없습니다."
        result[422] = _problem_response(
            description="본문 객체·reason·경로가 유효하지 않거나 추가 필드가 포함되어 있습니다.",
            example={
                "type": "/problems/validation-error",
                "title": "유효하지 않은 요청",
                "status": 422,
                "detail": "하나 이상의 요청 필드가 유효하지 않습니다.",
                "code": "VALIDATION_ERROR",
                "violations": [
                    {"field": "body.reason", "message": "유효한 변경 사유를 입력해야 합니다."}
                ],
            },
        )
    instance = f"/api/v1/master-codes/01890f7c-8abc-7def-8abc-222222222222/{action}"
    for response in result.values():
        media = response.get("content", {}).get("application/problem+json", {})
        if "example" in media:
            media["example"]["instance"] = instance
        for example in media.get("examples", {}).values():
            example["value"]["instance"] = instance
    if action == "tombstone":
        result[422]["content"]["application/problem+json"]["example"]["instance"] = (
            "/api/v1/master-codes/not-a-uuid/tombstone"
        )
    return result


def build_master_code_lifecycle_router(
    *,
    get_tombstone: GetMasterCodeTombstone,
    delete: DeleteMasterCode,
    restore: RestoreMasterCode,
    principal_dependency: Callable[..., HumanPrincipal],
    authorization: AuthorizationPolicy,
) -> APIRouter:
    router = APIRouter(prefix=f"{API_V1_PREFIX}/master-codes", tags=["MasterCodes"])
    read_guard = build_authorization_guard(
        principal_dependency, authorization, AuthorizationAction.READ_TOMBSTONE
    )
    mutation_guard = build_authorization_guard(
        principal_dependency, authorization, AuthorizationAction.MUTATE_DATA
    )

    @router.get(
        "/{master_code_id}/tombstone",
        operation_id="get_master_code_tombstone",
        response_model=MasterCodeTombstoneResponse,
        status_code=200,
        summary="삭제된 MasterCode 단건 조회",
        description=(
            "ADMIN 또는 SUPER_ADMIN이 삭제된 MasterCode와 보존된 참조의 현재 값을 "
            "조회합니다. 삭제된 Dimension도 포함하며, 복원에 사용할 강한 ETag를 "
            "반환합니다. 활성 행은 409, 없는 행은 404입니다."
        ),
        responses=_responses("tombstone"),
    )
    async def tombstone_route(
        master_code_id: Annotated[UUID, Path(description="조회할 MasterCode UUID입니다.")],
        response: Response,
        principal: Annotated[HumanPrincipal, Depends(read_guard)],
    ) -> MasterCodeTombstoneResponse:
        current = await get_tombstone.execute(principal, master_code_id)
        response.headers["ETag"] = current.etag()
        return _tombstone_response(current)

    @router.post(
        "/{master_code_id}/delete",
        operation_id="delete_master_code",
        response_model=MasterCodeTombstoneResponse,
        status_code=200,
        summary="MasterCode 논리 삭제",
        description=(
            "ADMIN 또는 SUPER_ADMIN이 강한 If-Match를 제공해 MasterCode를 논리 삭제합니다. "
            "참조와 합성 코드는 보존하고 version을 한 번 증가시키며 감사 이력을 함께 "
            "기록합니다. JSON 객체 본문이 필수이며 사유가 없으면 {}를 전송합니다. "
            "이미 삭제된 행과 없는 행은 404입니다."
        ),
        responses=_responses("delete"),
    )
    async def delete_route(
        master_code_id: Annotated[UUID, Path(description="삭제할 MasterCode UUID입니다.")],
        payload: MasterCodeLifecycleRequest,
        request: Request,
        response: Response,
        principal: Annotated[HumanPrincipal, Depends(mutation_guard)],
        if_match: MasterCodeIfMatchHeader,
    ) -> MasterCodeTombstoneResponse:
        expected = _parse_master_code_if_match_values(request.headers.getlist("if-match"))
        try:
            current = await delete.execute(
                principal, master_code_id, expected_etag=expected, reason=payload.reason
            )
        except DimensionValidationError as error:
            raise DimensionValidationError(f"body.{error.field}", error.message) from error
        response.headers["ETag"] = current.etag()
        return _tombstone_response(current)

    @router.post(
        "/{master_code_id}/restore",
        operation_id="restore_master_code",
        response_model=MasterCodeResponse,
        status_code=200,
        summary="MasterCode 복원",
        description=(
            "ADMIN 또는 SUPER_ADMIN이 삭제 응답이나 tombstone 조회에서 받은 ETag를 "
            "If-Match로 제공해 복원합니다. 모든 참조 Dimension이 활성 상태여야 하며 "
            "현재 대표 코드로 재합성하고 version을 한 번 증가시킵니다. JSON 객체 본문이 "
            "필수이며 사유가 없으면 {}를 전송합니다. 이미 활성인 행은 409입니다. "
            "삭제된 참조로 복원이 거부되면 violations.field는 company_id, brand_id, "
            "model_id, category_id, year_id, memory_id, network_id, country_id 중 "
            "해당 자리를 반환합니다. 예를 들어 company_id는 보존된 Company 참조를 뜻합니다."
        ),
        responses=_responses("restore"),
    )
    async def restore_route(
        master_code_id: Annotated[UUID, Path(description="복원할 MasterCode UUID입니다.")],
        payload: MasterCodeLifecycleRequest,
        request: Request,
        response: Response,
        principal: Annotated[HumanPrincipal, Depends(mutation_guard)],
        if_match: MasterCodeIfMatchHeader,
    ) -> MasterCodeResponse:
        expected = _parse_master_code_if_match_values(request.headers.getlist("if-match"))
        try:
            current = await restore.execute(
                principal, master_code_id, expected_etag=expected, reason=payload.reason
            )
        except DimensionValidationError as error:
            raise DimensionValidationError(f"body.{error.field}", error.message) from error
        response.headers["ETag"] = current.etag()
        return _response(current)

    return router


def _tombstone_response(current: MasterCode) -> MasterCodeTombstoneResponse:
    if current.deleted_at is None:
        raise ValueError("a MasterCode tombstone must be deleted")
    return MasterCodeTombstoneResponse(
        id=current.id,
        code=current.code,
        version=current.version,
        created_at=current.created_at,
        updated_at=current.updated_at,
        deleted_at=current.deleted_at,
        dimensions=MasterCodeDimensionsResponse(
            **{
                slot: _dimension_response(getattr(current.dimensions, slot))
                for slot in MasterCodeDimensions.ORDER
            }
        ),
    )
