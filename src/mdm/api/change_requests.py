"""HTTP contracts for submitting, reading and reviewing MasterCode requests."""

import base64
import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Body, Depends, Path, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictStr, model_validator
from pydantic.json_schema import SkipJsonSchema

from mdm.api.auth import INVALID_ACCESS_TOKEN_RESPONSE
from mdm.api.authorization import AUTHORIZATION_DENIED_RESPONSE, build_authorization_guard
from mdm.api.errors import problem_response
from mdm.api.master_codes import (
    ETAG_EXAMPLE,
    MASTER_CODE_CREATE_EXAMPLE,
    MasterCodeDimensionsInput,
    MemorySelectionInput,
    NetworkSelectionInput,
    ReferenceUpdateSelectionInput,
    StringSelectionInput,
    YearSelectionInput,
)
from mdm.api.routes import API_V1_PREFIX
from mdm.api.schemas import ProblemDetails
from mdm.application.auth import HumanPrincipal
from mdm.application.authorization import AuthorizationAction, AuthorizationPolicy
from mdm.application.change_requests import (
    ChangeRequestCursor,
    ChangeRequestNoEffect,
    ChangeRequestNotFound,
    ChangeRequestPage,
    ChangeRequestRestoreMismatch,
    ChangeRequestStaleTarget,
    ChangeRequestUseCases,
    validate_proposal,
)
from mdm.domain.change_requests import (
    ChangeOperation,
    ChangeProposal,
    ChangeRequest,
    ChangeRequestAlreadyReviewed,
    ChangeRequestStatus,
    ChangeRequestValidationError,
    ProposalPayload,
    ReviewMessage,
)

BASE = "/master-code-change-requests"
ADMIN_BASE = "/admin/master-code-change-requests"
EXAMPLE_ID = "01a088e9-e4e8-7000-8000-000000000009"
OriginalOperation = Literal["CREATE", "REFERENCE_UPDATE", "DELETE"]


class StrictInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProposedReferenceChanges(StrictInput):
    """변경할 자리만 지정하며 CREATE는 승인 트랜잭션에서 실행합니다."""

    company: Annotated[
        StringSelectionInput | SkipJsonSchema[None],
        Field(description="변경할 Company 자리이며 생략하면 현재 참조를 유지합니다."),
    ] = None
    brand: Annotated[
        StringSelectionInput | SkipJsonSchema[None],
        Field(description="변경할 Brand 자리이며 생략하면 현재 참조를 유지합니다."),
    ] = None
    model: Annotated[
        StringSelectionInput | SkipJsonSchema[None],
        Field(description="변경할 Model 자리이며 생략하면 현재 참조를 유지합니다."),
    ] = None
    category: Annotated[
        StringSelectionInput | SkipJsonSchema[None],
        Field(description="변경할 Category 자리이며 생략하면 현재 참조를 유지합니다."),
    ] = None
    year: Annotated[
        YearSelectionInput | SkipJsonSchema[None],
        Field(description="변경할 Year 자리이며 생략하면 현재 참조를 유지합니다."),
    ] = None
    memory: Annotated[
        MemorySelectionInput | SkipJsonSchema[None],
        Field(description="변경할 Memory 자리이며 생략하면 현재 참조를 유지합니다."),
    ] = None
    network: Annotated[
        NetworkSelectionInput | SkipJsonSchema[None],
        Field(description="변경할 Network 자리이며 생략하면 현재 참조를 유지합니다."),
    ] = None
    country: Annotated[
        StringSelectionInput | SkipJsonSchema[None],
        Field(description="변경할 Country 자리이며 생략하면 현재 참조를 유지합니다."),
    ] = None

    @model_validator(mode="after")
    def require_changes(self) -> "ProposedReferenceChanges":
        if not self.model_fields_set or any(
            getattr(self, key) is None for key in self.model_fields_set
        ):
            raise ValueError(
                "변경할 자리를 하나 이상 입력하며 null 대신 NOT_APPLICABLE을 사용해야 합니다."
            )
        return self


class RestoreReferences(StrictInput):
    """복원할 삭제 행과 정확히 같아야 하는 8개 참조입니다."""

    company: Annotated[
        ReferenceUpdateSelectionInput, Field(description="삭제 행의 Company 참조입니다.")
    ]
    brand: Annotated[
        ReferenceUpdateSelectionInput, Field(description="삭제 행의 Brand 참조입니다.")
    ]
    model: Annotated[
        ReferenceUpdateSelectionInput, Field(description="삭제 행의 Model 참조입니다.")
    ]
    category: Annotated[
        ReferenceUpdateSelectionInput, Field(description="삭제 행의 Category 참조입니다.")
    ]
    year: Annotated[ReferenceUpdateSelectionInput, Field(description="삭제 행의 Year 참조입니다.")]
    memory: Annotated[
        ReferenceUpdateSelectionInput, Field(description="삭제 행의 Memory 참조입니다.")
    ]
    network: Annotated[
        ReferenceUpdateSelectionInput, Field(description="삭제 행의 Network 참조입니다.")
    ]
    country: Annotated[
        ReferenceUpdateSelectionInput, Field(description="삭제 행의 Country 참조입니다.")
    ]


class CreateProposalPayload(StrictInput):
    dimensions: Annotated[
        MasterCodeDimensionsInput, Field(description="생성할 조합의 8개 자리이며 모두 필수입니다.")
    ]


class UpdateProposalPayload(StrictInput):
    dimensions: Annotated[
        ProposedReferenceChanges, Field(description="변경할 자리만 지정하며 나머지는 유지합니다.")
    ]


class RestoreProposalPayload(StrictInput):
    dimensions: Annotated[
        RestoreReferences,
        Field(
            description="삭제 행과 같은 8개 참조를 모두 입력합니다. 신규 생성은 허용하지 않습니다."
        ),
    ]


ExpectedEtag = Annotated[
    StrictStr,
    Field(
        pattern=r'^"mc-[1-9][0-9]*-[0-9a-f]{64}"$',
        description=(
            "대상 MasterCode 응답의 따옴표를 포함한 강한 ETag입니다. "
            "제출 때는 형식만, 승인 때는 현재 상태와 일치하는지 검사합니다."
        ),
        examples=[ETAG_EXAMPLE],
    ),
]


class CreateChangeProposal(StrictInput):
    operation: Annotated[
        Literal["CREATE"], Field(description="새 MasterCode 조합 생성 제안입니다.")
    ]
    target_id: Annotated[
        None, Field(description="생성에는 기존 대상이 없으므로 생략하거나 null입니다.")
    ] = None
    expected_etag: Annotated[
        None, Field(description="생성에는 대상 ETag가 없으므로 생략하거나 null입니다.")
    ] = None
    payload: Annotated[
        CreateProposalPayload, Field(description="생성할 Dimension 조합의 입력입니다.")
    ]


class UpdateChangeProposal(StrictInput):
    operation: Annotated[
        Literal["REFERENCE_UPDATE"],
        Field(description="기존 MasterCode의 일부 참조 변경 제안입니다."),
    ]
    target_id: Annotated[UUID, Field(description="참조를 바꿀 MasterCode UUID입니다.")]
    expected_etag: ExpectedEtag
    payload: Annotated[
        UpdateProposalPayload,
        Field(description="변경할 자리의 기존 참조, 신규 생성 또는 해당 없음 제안입니다."),
    ]


class DeleteChangeProposal(StrictInput):
    operation: Annotated[
        Literal["DELETE"], Field(description="기존 MasterCode의 논리 삭제 제안입니다.")
    ]
    target_id: Annotated[UUID, Field(description="논리 삭제할 MasterCode UUID입니다.")]
    expected_etag: ExpectedEtag
    payload: Annotated[
        None, Field(description="삭제에는 Dimension 입력이 없으므로 생략하거나 null입니다.")
    ] = None


class RestoreChangeProposal(StrictInput):
    operation: Annotated[
        Literal["RESTORE"],
        Field(description="CREATE 요청 대신 기존 삭제 행을 복원하는 관리자 적용안입니다."),
    ]
    target_id: Annotated[UUID, Field(description="복원할 삭제 MasterCode UUID입니다.")]
    expected_etag: Annotated[
        ExpectedEtag,
        Field(
            description="복원할 삭제된 MasterCode 응답의 따옴표를 포함한 강한 ETag입니다. "
            "승인 시 현재 상태와 일치하는지 검사합니다."
        ),
    ]
    payload: Annotated[
        RestoreProposalPayload, Field(description="대상 삭제 행과 정확히 같은 참조 조합입니다.")
    ]


OriginalProposalInput = Annotated[
    CreateChangeProposal | UpdateChangeProposal | DeleteChangeProposal,
    Field(discriminator="operation"),
]
ApprovalProposalInput = Annotated[
    CreateChangeProposal | UpdateChangeProposal | DeleteChangeProposal | RestoreChangeProposal,
    Field(discriminator="operation"),
]


class SubmitChangeRequestInput(StrictInput):
    proposal: Annotated[
        OriginalProposalInput, Field(description="수정되지 않고 영구 보존할 원본 제안입니다.")
    ]
    reason: Annotated[
        StrictStr | None,
        Field(
            description=(
                "요청자가 남기는 선택적 설명입니다. 앞뒤 일반 공백을 제거해 저장하며 "
                "빈 문자열과 공백만 있는 문자열은 null로 저장합니다. 정규화 후 500자 "
                "이하이며 제어문자는 금지합니다. 관리자 감사 로그의 reason으로 "
                "복사하지 않습니다."
            ),
            json_schema_extra={"x-normalized-maxLength": 500},
        ),
    ] = None


ReviewMessageInput = Annotated[
    StrictStr,
    Field(
        min_length=1,
        description=(
            "앞뒤 일반 공백 제거 후 1~500자의 검토 메시지입니다. "
            "탭·줄바꿈·제어문자는 허용하지 않습니다. 수정 승인과 거절에는 필수이며 "
            "요청자가 결과 조회에서 확인할 수 있습니다."
        ),
        json_schema_extra={"x-normalized-maxLength": 500},
    ),
]


class ApproveChangeRequestInput(StrictInput):
    approved_proposal: Annotated[
        ApprovalProposalInput | SkipJsonSchema[None],
        Field(
            description=(
                "생략하면 원안 승인입니다. 지정하면 원본에 자동 병합하지 않고 완전한 "
                "적용안으로 검토합니다. null은 허용하지 않습니다."
            )
        ),
    ] = None
    review_message: Annotated[
        ReviewMessageInput | SkipJsonSchema[None],
        Field(
            description=(
                "원안 승인에서만 생략할 수 있는 검토 메시지입니다. 앞뒤 일반 공백 제거 후 "
                "1~500자이며 null·빈 메시지·탭·줄바꿈·제어문자는 허용하지 않습니다."
            )
        ),
    ] = None

    @model_validator(mode="after")
    def validate_optional_fields(self) -> "ApproveChangeRequestInput":
        if any(getattr(self, key) is None for key in self.model_fields_set):
            raise ValueError("선택 필드는 생략할 수 있지만 null은 허용하지 않습니다.")
        if self.review_message is not None:
            ReviewMessage(self.review_message)
        return self


class RejectChangeRequestInput(StrictInput):
    review_message: ReviewMessageInput


class SubmittedChangeRequestResponse(BaseModel):
    id: Annotated[UUID, Field(description="접수한 변경 요청의 UUID입니다.")]
    status: Annotated[Literal["PENDING"], Field(description="관리자 검토를 기다리는 상태입니다.")]
    created_at: Annotated[datetime, Field(description="요청이 접수된 시각입니다.")]


class ChangeRequestResponse(BaseModel):
    id: Annotated[UUID, Field(description="변경 요청의 UUID입니다.")]
    status: Annotated[
        ChangeRequestStatus,
        Field(description="현재 요청 처리 상태입니다. 최종 상태는 다시 처리할 수 없습니다."),
    ]
    original: Annotated[
        OriginalProposalInput,
        Field(description="입력 당시의 code·value 문자열을 보존한 불변 원본 제안입니다."),
    ]
    requester_id: Annotated[UUID, Field(description="원본을 제출한 사용자의 UUID입니다.")]
    reason: Annotated[
        str | None,
        Field(
            description="요청자가 제출한 설명의 앞뒤 일반 공백을 제거한 값입니다. "
            "설명이 없거나 빈 문자열 또는 공백만 제출했으면 null입니다."
        ),
    ]
    created_at: Annotated[datetime, Field(description="원본 요청 접수 시각입니다.")]
    approved_proposal: Annotated[
        ApprovalProposalInput | None,
        Field(
            description=(
                "실제 적용된 제안입니다. PENDING과 REJECTED에서는 "
                "null입니다. CREATE의 target_id는 null입니다."
            )
        ),
    ]
    reviewer_id: Annotated[
        UUID | None, Field(description="승인 또는 거절한 관리자의 UUID이며 검토 전에는 null입니다.")
    ]
    reviewed_at: Annotated[
        datetime | None, Field(description="승인 또는 거절 시각이며 검토 전에는 null입니다.")
    ]
    review_message: Annotated[
        str | None,
        Field(
            description=(
                "관리자가 남긴 메시지입니다. 원안 승인에서 생략했거나 검토 전이면 null입니다."
            )
        ),
    ]
    applied_change_set_id: Annotated[
        UUID | None,
        Field(
            description=(
                "승인으로 생성된 모든 DimensionLog·MasterCodeLog를 "
                "연결하는 change set UUID입니다. PENDING과 "
                "REJECTED에서는 null입니다."
            )
        ),
    ]


class ChangeRequestListResponse(BaseModel):
    items: Annotated[
        list[ChangeRequestResponse],
        Field(description="접수 시각 내림차순, 같은 시각에는 UUID 내림차순인 요청 목록입니다."),
    ]
    next_cursor: Annotated[
        str | None,
        Field(
            description=(
                "다음 페이지 조회에 그대로 전달할 cursor입니다. 더 이상 결과가 없으면 null입니다."
            )
        ),
    ]


CHANGE_REQUEST_ERRORS: dict[type[Exception], tuple[int, str, str]] = {
    ChangeRequestNotFound: (404, "CHANGE_REQUEST_NOT_FOUND", "요청을 찾을 수 없습니다."),
    ChangeRequestAlreadyReviewed: (
        409,
        "CHANGE_REQUEST_ALREADY_REVIEWED",
        "이미 승인 또는 거절된 요청입니다.",
    ),
    ChangeRequestNoEffect: (
        409,
        "CHANGE_REQUEST_NO_EFFECT",
        (
            "적용할 변경이 없습니다. 요청은 PENDING으로 유지되며 메시지를 "
            "포함한 별도 거절이 필요합니다."
        ),
    ),
    ChangeRequestStaleTarget: (
        409,
        "CHANGE_REQUEST_STALE_TARGET",
        (
            "대상 상태가 적용 기준 ETag와 다릅니다. 최신 상태를 검토한 뒤 수정"
            " 승인하거나 거절해 주세요."
        ),
    ),
    ChangeRequestRestoreMismatch: (
        409,
        "CHANGE_REQUEST_RESTORE_MISMATCH",
        "복원 적용안의 참조 조합이 대상 삭제 행과 다릅니다.",
    ),
    ChangeRequestValidationError: (
        422,
        "VALIDATION_ERROR",
        "변경 요청 또는 검토 입력이 유효하지 않습니다.",
    ),
}


def change_request_error_handler(request: Request, exc: Exception) -> JSONResponse:
    status, code, detail = CHANGE_REQUEST_ERRORS[type(exc)]
    if isinstance(exc, ChangeRequestValidationError):
        detail = str(exc)
    return problem_response(
        ProblemDetails(
            type=f"/problems/{code.lower().replace('_', '-')}",
            title="변경 요청 처리 오류",
            status=status,
            code=code,
            detail=detail,
            instance=request.url.path,
        )
    )


def _errors(
    path: str, *, detail: bool = False, review: bool = False, approval: bool = False
) -> dict[int | str, dict[str, Any]]:
    codes = [
        (422, "VALIDATION_ERROR", "요청 입력이 유효하지 않습니다."),
        (503, "SERVICE_UNAVAILABLE", "일시적으로 요청을 처리할 수 없습니다."),
    ]
    if detail or review:
        codes.append(CHANGE_REQUEST_ERRORS[ChangeRequestNotFound])
    if review:
        codes.append(CHANGE_REQUEST_ERRORS[ChangeRequestAlreadyReviewed])
    if approval:
        codes.extend(
            CHANGE_REQUEST_ERRORS[error]
            for error in (
                ChangeRequestNoEffect,
                ChangeRequestStaleTarget,
                ChangeRequestRestoreMismatch,
            )
        )
        codes.extend(
            [
                (404, "MASTER_CODE_NOT_FOUND", "대상 MasterCode를 찾을 수 없습니다."),
                (409, "MASTER_CODE_CONFLICT", "MasterCode 조합이 이미 사용 중입니다."),
                (409, "MASTER_CODE_NOT_DELETED", "복원 대상이 삭제 상태가 아닙니다."),
                (
                    409,
                    "MASTER_CODE_REFERENCE_INACTIVE",
                    "삭제된 Dimension 참조가 있어 복원할 수 없습니다.",
                ),
                (409, "DIMENSION_CODE_CONFLICT", "신규 Dimension code가 이미 사용 중입니다."),
                (409, "DIMENSION_VALUE_CONFLICT", "신규 Dimension value가 이미 사용 중입니다."),
                (
                    409,
                    "DIMENSION_MULTIPLE_CONFLICTS",
                    "신규 Dimension code와 value가 이미 사용 중입니다.",
                ),
                (
                    422,
                    "INVALID_DIMENSION_REFERENCE",
                    "존재하지 않거나 삭제된 Dimension 참조입니다.",
                ),
            ]
        )
    responses: dict[int | str, dict[str, Any]] = {
        401: INVALID_ACCESS_TOKEN_RESPONSE,
        403: AUTHORIZATION_DENIED_RESPONSE,
    }
    for status, code, message in codes:
        response = responses.setdefault(
            status,
            {
                "model": ProblemDetails,
                "description": (
                    "일시적으로 서비스를 이용할 수 없습니다. 잠시 후 다시 시도해 주세요."
                    if status == 503
                    else (
                        "입력·대상 상태를 확인해 주세요. "
                        "실패한 요청은 데이터와 처리 상태를 변경하지 않습니다."
                    )
                ),
                "content": {
                    "application/problem+json": {
                        "schema": {"$ref": "#/components/schemas/ProblemDetails"},
                        "examples": {},
                    }
                },
            },
        )
        response["content"]["application/problem+json"]["examples"][code] = {
            "summary": message,
            "value": {
                "type": f"/problems/{code.lower().replace('_', '-')}",
                "title": "요청 처리 오류",
                "status": status,
                "detail": message,
                "code": code,
                "instance": f"{API_V1_PREFIX}{path}",
            },
        }
    return responses


def build_change_request_router(
    *,
    use_cases: ChangeRequestUseCases,
    principal_dependency: Callable[..., HumanPrincipal],
    authorization: AuthorizationPolicy,
) -> APIRouter:
    router = APIRouter(prefix=API_V1_PREFIX, tags=["MasterCodeChangeRequests"])
    submit_guard = build_authorization_guard(
        principal_dependency, authorization, AuthorizationAction.SUBMIT_CREATE_REQUEST
    )
    read_guard = build_authorization_guard(
        principal_dependency, authorization, AuthorizationAction.READ_DATA
    )
    review_guard = build_authorization_guard(
        principal_dependency, authorization, AuthorizationAction.REVIEW_CHANGE_REQUEST
    )

    @router.post(
        BASE,
        operation_id="submit_master_code_change_request",
        response_model=SubmittedChangeRequestResponse,
        status_code=201,
        summary="MasterCode 변경 요청 제출",
        description=(
            "USER가 생성·참조 수정·삭제를 제안합니다. 입력 형식만 검증하고 "
            "PENDING으로 접수하며 대상 존재·중복·최신 ETag는 승인 시 "
            "검사합니다. ADMIN과 SUPER_ADMIN은 제출할 수 없습니다."
        ),
        responses=_errors(BASE),
    )
    async def submit(
        payload: Annotated[
            SubmitChangeRequestInput,
            Body(
                openapi_examples={
                    "create": {
                        "summary": "기존 참조와 신규 Dimension을 포함한 생성 제안",
                        "value": {
                            "proposal": {
                                "operation": "CREATE",
                                "payload": {"dimensions": MASTER_CODE_CREATE_EXAMPLE["dimensions"]},
                            },
                            "reason": "신규 제품 등록 요청",
                        },
                    },
                }
            ),
        ],
        principal: Annotated[HumanPrincipal, Depends(submit_guard)],
    ) -> SubmittedChangeRequestResponse:
        request = await use_cases.submit(
            principal, _to_proposal(payload.proposal), reason=payload.reason
        )
        return SubmittedChangeRequestResponse(
            id=request.id, status="PENDING", created_at=request.created_at
        )

    @router.get(
        BASE,
        operation_id="list_own_master_code_change_requests",
        response_model=ChangeRequestListResponse,
        response_model_exclude_unset=True,
        status_code=200,
        summary="본인 변경 요청 목록",
        description=(
            "본인이 제출한 요청을 조회합니다. 관리자 승격 후에도 사용할 수 "
            "있습니다. status 생략 시 모든 상태를 포함하며 operation은"
            " 원본 작업 종류입니다. 페이지를 이동할 때 같은 필터를 유지해 주세요."
        ),
        responses=_errors(BASE),
    )
    async def list_own(
        principal: Annotated[HumanPrincipal, Depends(read_guard)],
        status: Annotated[
            ChangeRequestStatus | None,
            Query(description="요청 상태 필터이며 생략하면 모든 상태입니다."),
        ] = None,
        operation: Annotated[
            OriginalOperation | None, Query(description="원본 작업 종류 필터입니다.")
        ] = None,
        cursor: Annotated[
            str | None, Query(max_length=512, description="직전 응답의 next_cursor입니다.")
        ] = None,
        limit: Annotated[
            int, Query(ge=1, le=100, description="한 페이지의 최대 요청 수입니다.")
        ] = 20,
    ) -> ChangeRequestListResponse:
        return _page(
            await use_cases.list(
                principal,
                for_review=False,
                status=status,
                operation=ChangeOperation(operation) if operation else None,
                after=_decode_cursor(cursor) if cursor else None,
                limit=limit,
            )
        )

    @router.get(
        f"{BASE}/{{request_id}}",
        operation_id="get_own_master_code_change_request",
        response_model=ChangeRequestResponse,
        response_model_exclude_unset=True,
        status_code=200,
        summary="본인 변경 요청 상세",
        description=(
            "본인 요청의 원본·처리 상태·관리자 메시지와 적용안을 조회합니다. 다른 "
            "사용자의 요청은 존재 여부를 숨기는 404입니다. 이메일·푸시 알림은 "
            "전송하지 않습니다."
        ),
        responses=_errors(f"{BASE}/{EXAMPLE_ID}", detail=True),
    )
    async def get_own(
        request_id: Annotated[UUID, Path(description="변경 요청 UUID입니다.")],
        principal: Annotated[HumanPrincipal, Depends(read_guard)],
    ) -> ChangeRequestResponse:
        return _response(await use_cases.get_own(principal, request_id))

    @router.get(
        ADMIN_BASE,
        operation_id="list_master_code_change_review_queue",
        response_model=ChangeRequestListResponse,
        response_model_exclude_unset=True,
        status_code=200,
        summary="관리자 변경 요청 검토 목록",
        description=(
            "ADMIN·SUPER_ADMIN이 요청을 조회합니다. 기본 "
            "PENDING이며 status와 원본 operation으로 필터링합니다."
            " 접수 시각과 UUID의 내림차순 cursor를 사용합니다. 페이지 이동"
            " 시 같은 필터를 유지해 주세요."
        ),
        responses=_errors(ADMIN_BASE),
    )
    async def list_review(
        principal: Annotated[HumanPrincipal, Depends(review_guard)],
        status: Annotated[
            ChangeRequestStatus, Query(description="요청 상태 필터이며 기본 PENDING입니다.")
        ] = ChangeRequestStatus.PENDING,
        operation: Annotated[
            OriginalOperation | None, Query(description="원본 작업 종류 필터입니다.")
        ] = None,
        cursor: Annotated[
            str | None, Query(max_length=512, description="직전 응답의 next_cursor입니다.")
        ] = None,
        limit: Annotated[
            int, Query(ge=1, le=100, description="한 페이지의 최대 요청 수입니다.")
        ] = 20,
    ) -> ChangeRequestListResponse:
        return _page(
            await use_cases.list(
                principal,
                for_review=True,
                status=status,
                operation=ChangeOperation(operation) if operation else None,
                after=_decode_cursor(cursor) if cursor else None,
                limit=limit,
            )
        )

    @router.get(
        f"{ADMIN_BASE}/{{request_id}}",
        operation_id="get_master_code_change_request_for_review",
        response_model=ChangeRequestResponse,
        response_model_exclude_unset=True,
        status_code=200,
        summary="관리자 변경 요청 상세",
        description=(
            "ADMIN·SUPER_ADMIN이 원본과 처리 결과를 검토합니다. 최신 "
            "대상 상태와 ETag는 일반 MasterCode 단건 조회 또는 관리자용 "
            "삭제된 MasterCode 조회로 확인합니다."
        ),
        responses=_errors(f"{ADMIN_BASE}/{EXAMPLE_ID}", detail=True),
    )
    async def get_review(
        request_id: Annotated[UUID, Path(description="검토할 변경 요청 UUID입니다.")],
        principal: Annotated[HumanPrincipal, Depends(review_guard)],
    ) -> ChangeRequestResponse:
        return _response(await use_cases.get_for_review(principal, request_id))

    @router.post(
        f"{ADMIN_BASE}/{{request_id}}/approval",
        operation_id="approve_master_code_change_request",
        response_model=ChangeRequestResponse,
        response_model_exclude_unset=True,
        status_code=200,
        summary="변경 요청 승인",
        description=(
            "ADMIN·SUPER_ADMIN이 요청을 잠근 뒤 "
            "Dimension·MasterCode·로그와 승인 결과를 함께 "
            "반영합니다. {}는 원안 승인입니다. 수정안은 원본과 자동 병합하지 "
            "않으며 ETag만 바꿔도 메시지가 필요합니다. 적용할 변경이 없으면 "
            "409를 반환하고 PENDING을 유지하며 별도 거절이 필요합니다. CREATE 대신 같은 "
            "참조 조합의 삭제 행을 RESTORE하는 변경만 operation·대상 "
            "변경의 예외입니다."
        ),
        responses=_errors(f"{ADMIN_BASE}/{EXAMPLE_ID}/approval", review=True, approval=True),
    )
    async def approve(
        request_id: Annotated[UUID, Path(description="승인할 변경 요청 UUID입니다.")],
        payload: Annotated[
            ApproveChangeRequestInput,
            Body(
                openapi_examples={
                    "original": {"summary": "원안 승인", "value": {}},
                    "withMessage": {
                        "summary": "원안 승인과 메시지",
                        "value": {"review_message": "내용을 확인했습니다."},
                    },
                }
            ),
        ],
        principal: Annotated[HumanPrincipal, Depends(review_guard)],
    ) -> ChangeRequestResponse:
        proposal = (
            _to_proposal(payload.approved_proposal)
            if payload.approved_proposal is not None
            else None
        )
        return _response(
            await use_cases.approve(
                principal,
                request_id,
                approved_proposal=proposal,
                message=ReviewMessage(payload.review_message)
                if payload.review_message is not None
                else None,
            )
        )

    @router.post(
        f"{ADMIN_BASE}/{{request_id}}/rejection",
        operation_id="reject_master_code_change_request",
        response_model=ChangeRequestResponse,
        response_model_exclude_unset=True,
        status_code=200,
        summary="변경 요청 거절",
        description=(
            "ADMIN·SUPER_ADMIN이 필수 검토 메시지와 함께 요청을 "
            "REJECTED로 확정합니다. Dimension·MasterCode와 "
            "감사 로그를 변경하지 않습니다. 최종 상태의 요청은 다시 처리할 수 "
            "없습니다."
        ),
        responses=_errors(f"{ADMIN_BASE}/{EXAMPLE_ID}/rejection", review=True),
    )
    async def reject(
        request_id: Annotated[UUID, Path(description="거절할 변경 요청 UUID입니다.")],
        payload: Annotated[RejectChangeRequestInput, Body()],
        principal: Annotated[HumanPrincipal, Depends(review_guard)],
    ) -> ChangeRequestResponse:
        return _response(
            await use_cases.reject(
                principal, request_id, message=ReviewMessage(payload.review_message)
            )
        )

    return router


def _to_proposal(
    value: CreateChangeProposal
    | UpdateChangeProposal
    | DeleteChangeProposal
    | RestoreChangeProposal,
) -> ChangeProposal:
    proposal = ChangeProposal(
        ChangeOperation(value.operation),
        value.target_id,
        ProposalPayload.from_dict(value.payload.model_dump(mode="json", exclude_unset=True))
        if value.payload is not None
        else None,
        value.expected_etag,
    )
    validate_proposal(proposal)
    return proposal


def _proposal_dict(proposal: ChangeProposal) -> dict[str, Any]:
    return {
        "operation": proposal.operation.value,
        "target_id": proposal.target_id,
        "expected_etag": proposal.expected_etag,
        "payload": proposal.payload.to_dict() if proposal.payload else None,
    }


def _response(request: ChangeRequest) -> ChangeRequestResponse:
    return ChangeRequestResponse.model_validate(
        {
            "id": request.id,
            "status": request.status,
            "original": _proposal_dict(request.original),
            "requester_id": request.requester_id,
            "reason": request.reason,
            "created_at": request.created_at,
            "approved_proposal": _proposal_dict(request.approved) if request.approved else None,
            "reviewer_id": request.reviewer_id,
            "reviewed_at": request.reviewed_at,
            "review_message": request.review_message.value if request.review_message else None,
            "applied_change_set_id": request.applied_change_set_id,
        }
    )


def _page(page: ChangeRequestPage) -> ChangeRequestListResponse:
    cursor = None
    if page.has_more and page.items:
        last = page.items[-1]
        raw = json.dumps(
            {"v": 1, "t": last.created_at.astimezone(UTC).isoformat(), "i": str(last.id)},
            separators=(",", ":"),
        ).encode()
        cursor = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
    return ChangeRequestListResponse(
        items=[_response(item) for item in page.items], next_cursor=cursor
    )


def _decode_cursor(value: str) -> ChangeRequestCursor:
    try:
        raw = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
        decoded = json.loads(raw)
        if (
            not isinstance(decoded, dict)
            or set(decoded) != {"v", "t", "i"}
            or type(decoded["v"]) is not int
            or decoded["v"] != 1
        ):
            raise ValueError
        timestamp = datetime.fromisoformat(decoded["t"])
        if timestamp.utcoffset() is None:
            raise ValueError
        return ChangeRequestCursor(timestamp, UUID(decoded["i"]))
    except (ValueError, TypeError, UnicodeError, AttributeError):
        raise ChangeRequestValidationError("유효하지 않은 cursor입니다.") from None
