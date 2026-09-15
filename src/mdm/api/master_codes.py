"""HTTP contracts for MasterCode creation and active reads."""

import base64
import json
import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Body, Depends, Header, Path, Query, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, model_validator
from pydantic.json_schema import SkipJsonSchema

from mdm.api.auth import INVALID_ACCESS_TOKEN_RESPONSE
from mdm.api.authorization import AUTHORIZATION_DENIED_RESPONSE, build_authorization_guard
from mdm.api.routes import API_V1_PREFIX
from mdm.api.schemas import ProblemDetails
from mdm.application.auth import HumanPrincipal
from mdm.application.authorization import AuthorizationAction, AuthorizationPolicy
from mdm.application.master_codes import (
    CreateMasterCode,
    ExistingDimension,
    GetMasterCode,
    InlineDimension,
    ListMasterCodes,
    MasterCodeCreateInput,
    MasterCodeCursor,
    MasterCodePage,
    MasterCodeReferenceUpdate,
    MemoryCreateValue,
    NotApplicable,
    ReferenceSelection,
    UpdateMasterCodeReferences,
)
from mdm.application.preconditions import InvalidIfMatch, PreconditionRequired
from mdm.domain.dimensions import (
    DimensionValidationError,
    MemoryValue,
    NetworkGeneration,
    YearValue,
)
from mdm.domain.master_codes import MasterCode, MasterCodeDimension, MasterCodeDimensions

CURSOR_EXAMPLE = (
    "eyJpIjoiMDFhMDg4ZTktZTRlOC03MDAwLTgwMDAtMDAwMDAwMDAwMDA5IiwidCI6"
    "IjIwMjYtMDktMTBUMDE6MjM6NDVaIiwidiI6MX0"
)
ETAG_EXAMPLE = '"mc-1-f04134011a34cd1149678cf333ea35d4ed19223e32a9d4c6fe6fa5cdfe2aad1d"'
_MASTER_CODE_ETAG = re.compile(r'^"mc-([1-9][0-9]*)-([0-9a-f]{64})"$', re.ASCII)

MasterCodeIfMatchHeader = Annotated[
    str,
    Header(
        alias="If-Match",
        description=(
            "직전 MasterCode 단건 응답에서 받은, MasterCode와 여덟 자리의 현재 Dimension "
            "상태를 함께 반영한 강한 ETag입니다. 따옴표를 포함해 문서에 제시된 정확한 형식의 "
            "값 하나만 허용합니다."
        ),
        examples=[ETAG_EXAMPLE],
        json_schema_extra={"pattern": r'^"mc-[1-9][0-9]*-[0-9a-f]{64}"$'},
    ),
]

MASTER_CODE_CREATE_EXAMPLE: dict[str, Any] = {
    "dimensions": {
        "company": {
            "mode": "REFERENCE",
            "id": "01a088e9-e4e8-7000-8000-000000000001",
        },
        "brand": {"mode": "CREATE", "code": "BRA", "value": "Acme Brand"},
        "model": {"mode": "NOT_APPLICABLE"},
        "category": {
            "mode": "REFERENCE",
            "id": "01a088e9-e4e8-7000-8000-000000000003",
        },
        "year": {"mode": "CREATE", "code": "YR2026", "value": 2026},
        "memory": {
            "mode": "CREATE",
            "code": "MEM128",
            "value": {"amount": 128, "unit": "GB"},
        },
        "network": {"mode": "CREATE", "code": "NET5", "value": 5},
        "country": {"mode": "NOT_APPLICABLE"},
    },
    "reason": "신규 제품 조합 등록",
}

MASTER_CODE_RESPONSE_EXAMPLE: dict[str, Any] = {
    "id": "01a088e9-e4e8-7000-8000-000000000009",
    "code": "COM-BRA-NNN-CAT-YR2026-MEM128-NET5-NNN",
    "version": 1,
    "created_at": "2026-09-10T01:23:45Z",
    "updated_at": "2026-09-10T01:23:45Z",
    "deleted_at": None,
    "dimensions": {
        "company": {
            "id": "01a088e9-e4e8-7000-8000-000000000001",
            "code": "COM",
            "value": "ACME_CORP",
        },
        "brand": {
            "id": "01a088e9-e4e8-7000-8000-000000000002",
            "code": "BRA",
            "value": "ACME_BRAND",
        },
        "model": None,
        "category": {
            "id": "01a088e9-e4e8-7000-8000-000000000003",
            "code": "CAT",
            "value": "SMARTPHONE",
        },
        "year": {
            "id": "01a088e9-e4e8-7000-8000-000000000004",
            "code": "YR2026",
            "value": 2026,
        },
        "memory": {
            "id": "01a088e9-e4e8-7000-8000-000000000005",
            "code": "MEM128",
            "value": {"amount": 128, "unit": "GB", "capacity_mb": 128000},
        },
        "network": {
            "id": "01a088e9-e4e8-7000-8000-000000000006",
            "code": "NET5",
            "value": 5,
        },
        "country": None,
    },
}


class ReferenceInput(BaseModel):
    """기존 활성 Dimension 참조입니다."""

    model_config = ConfigDict(extra="forbid")
    mode: Annotated[
        Literal["REFERENCE"],
        Field(description="기존 활성 Dimension을 참조하는 입력 종류입니다."),
    ]
    id: Annotated[UUID, Field(description="참조할 활성 Dimension의 UUID입니다.")]


class NotApplicableInput(BaseModel):
    """해당 Dimension 자리를 명시적으로 사용하지 않습니다."""

    model_config = ConfigDict(extra="forbid")
    mode: Annotated[
        Literal["NOT_APPLICABLE"],
        Field(description="이 자리를 사용하지 않고 합성 코드에 NNN을 넣는 입력 종류입니다."),
    ]


def _code_field(example: str) -> Any:
    return Field(
        min_length=1,
        max_length=32,
        description=(
            "mode: CREATE로 생성할 Dimension의 대표 코드입니다. ASCII 소문자는 대문자로 "
            "바꾸지만 공백과 A-Z·0-9 이외 문자는 거부하며, 정규화 후 N으로만 이루어진 "
            "1~32자 값은 예약 코드라서 사용할 수 없습니다."
        ),
        examples=[example],
    )


class StringCreateInput(BaseModel):
    """문자열 값 Dimension의 mode: CREATE 입력입니다."""

    model_config = ConfigDict(extra="forbid")
    mode: Annotated[
        Literal["CREATE"],
        Field(description="문자열 Dimension을 같은 트랜잭션에서 생성하는 입력 종류입니다."),
    ]
    code: Annotated[StrictStr, _code_field("COM")]
    value: Annotated[
        StrictStr,
        Field(
            min_length=1,
            description=(
                "정규화할 문자열 Dimension 값입니다. 앞뒤 일반 공백을 제거하고 ASCII 소문자를 "
                "대문자로 바꾸며, 연속된 일반 공백과 밑줄을 밑줄 하나로 축약합니다. 정규화 "
                "결과는 A-Z·0-9와 단어 사이 밑줄만 사용한 1~128자여야 하며 탭, 줄바꿈, "
                "비 ASCII 문자와 특수문자는 거부합니다."
            ),
            examples=["ACME_CORP"],
            json_schema_extra={
                "x-normalized-pattern": "^[A-Z0-9]+(?:_[A-Z0-9]+)*$",
                "x-normalized-minLength": 1,
                "x-normalized-maxLength": 128,
            },
        ),
    ]


class YearCreateInput(BaseModel):
    """Year Dimension의 mode: CREATE 입력입니다."""

    model_config = ConfigDict(extra="forbid")
    mode: Annotated[
        Literal["CREATE"],
        Field(description="Year Dimension을 같은 트랜잭션에서 생성하는 입력 종류입니다."),
    ]
    code: Annotated[StrictStr, _code_field("YR2026")]
    value: Annotated[
        StrictInt,
        Field(
            ge=2000,
            le=2999,
            description="문자열·boolean·실수 변환을 허용하지 않는 연도 값입니다.",
            examples=[2026],
        ),
    ]


class NetworkCreateInput(BaseModel):
    """Network Dimension의 mode: CREATE 입력입니다."""

    model_config = ConfigDict(extra="forbid")
    mode: Annotated[
        Literal["CREATE"],
        Field(description="Network Dimension을 같은 트랜잭션에서 생성하는 입력 종류입니다."),
    ]
    code: Annotated[StrictStr, _code_field("NET5")]
    value: Annotated[
        StrictInt,
        Field(
            ge=1,
            le=5,
            description="문자열·boolean·실수 변환을 허용하지 않는 Network 세대입니다.",
            examples=[5],
        ),
    ]


class MasterCodeMemoryValueInput(BaseModel):
    """Memory Dimension의 mode: CREATE 생성 값입니다."""

    model_config = ConfigDict(extra="forbid")
    amount: Annotated[
        StrictInt,
        Field(
            ge=1,
            le=2_147_483_647,
            description="Memory 단위와 함께 해석할 양의 정수입니다.",
            examples=[128],
        ),
    ]
    unit: Annotated[
        StrictStr,
        Field(
            pattern=r"^ *[MmGgTtPp][Bb] *$",
            description="앞뒤 일반 공백과 ASCII 대소문자를 정규화하는 MB·GB·TB·PB 단위입니다.",
            examples=["GB"],
        ),
    ]


class MasterCodeMemoryCreateInput(BaseModel):
    """Memory Dimension의 mode: CREATE 입력입니다."""

    model_config = ConfigDict(extra="forbid")
    mode: Annotated[
        Literal["CREATE"],
        Field(description="Memory Dimension을 같은 트랜잭션에서 생성하는 입력 종류입니다."),
    ]
    code: Annotated[StrictStr, _code_field("MEM128")]
    value: Annotated[
        MasterCodeMemoryValueInput,
        Field(description="생성할 Memory의 양과 단위입니다."),
    ]


StringSelectionInput = Annotated[
    ReferenceInput | StringCreateInput | NotApplicableInput,
    Field(discriminator="mode"),
]
YearSelectionInput = Annotated[
    ReferenceInput | YearCreateInput | NotApplicableInput,
    Field(discriminator="mode"),
]
MemorySelectionInput = Annotated[
    ReferenceInput | MasterCodeMemoryCreateInput | NotApplicableInput,
    Field(discriminator="mode"),
]
NetworkSelectionInput = Annotated[
    ReferenceInput | NetworkCreateInput | NotApplicableInput,
    Field(discriminator="mode"),
]


class MasterCodeDimensionsInput(BaseModel):
    """합성에 사용할 MasterCode Dimension 8개 자리입니다.

    합성 순서는 company, brand, model, category, year, memory, network, country이며 각 대표
    코드를 하이픈으로 연결합니다. NOT_APPLICABLE 자리는 NNN으로 합성합니다.
    """

    model_config = ConfigDict(extra="forbid")
    company: Annotated[StringSelectionInput, Field(description="Company 자리 입력입니다.")]
    brand: Annotated[StringSelectionInput, Field(description="Brand 자리 입력입니다.")]
    model: Annotated[StringSelectionInput, Field(description="Model 자리 입력입니다.")]
    category: Annotated[StringSelectionInput, Field(description="Category 자리 입력입니다.")]
    year: Annotated[YearSelectionInput, Field(description="Year 자리 입력입니다.")]
    memory: Annotated[MemorySelectionInput, Field(description="Memory 자리 입력입니다.")]
    network: Annotated[NetworkSelectionInput, Field(description="Network 자리 입력입니다.")]
    country: Annotated[StringSelectionInput, Field(description="Country 자리 입력입니다.")]


class MasterCodeCreateRequest(BaseModel):
    """관리자 직접 MasterCode 생성 입력입니다."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={"example": MASTER_CODE_CREATE_EXAMPLE},
    )
    dimensions: Annotated[
        MasterCodeDimensionsInput,
        Field(description="빠짐없이 명시해야 하는 8개 고정 Dimension 자리입니다."),
    ]
    reason: Annotated[
        StrictStr | None,
        Field(
            description="생성 이유이며 앞뒤 일반 공백을 제거한 결과가 500자 이하여야 합니다.",
            json_schema_extra={"x-normalized-maxLength": 500},
        ),
    ] = None


ReferenceUpdateSelectionInput = Annotated[
    ReferenceInput | NotApplicableInput,
    Field(discriminator="mode"),
]


class MasterCodeReferenceUpdatesInput(BaseModel):
    """변경할 MasterCode Dimension 자리만 포함하는 부분 입력입니다."""

    model_config = ConfigDict(extra="forbid", json_schema_extra={"minProperties": 1})
    company: Annotated[
        ReferenceUpdateSelectionInput | SkipJsonSchema[None],
        Field(description="변경할 Company 참조이며 생략하면 현재 참조를 유지합니다."),
    ] = None
    brand: Annotated[
        ReferenceUpdateSelectionInput | SkipJsonSchema[None],
        Field(description="변경할 Brand 참조이며 생략하면 현재 참조를 유지합니다."),
    ] = None
    model: Annotated[
        ReferenceUpdateSelectionInput | SkipJsonSchema[None],
        Field(description="변경할 Model 참조이며 생략하면 현재 참조를 유지합니다."),
    ] = None
    category: Annotated[
        ReferenceUpdateSelectionInput | SkipJsonSchema[None],
        Field(description="변경할 Category 참조이며 생략하면 현재 참조를 유지합니다."),
    ] = None
    year: Annotated[
        ReferenceUpdateSelectionInput | SkipJsonSchema[None],
        Field(description="변경할 Year 참조이며 생략하면 현재 참조를 유지합니다."),
    ] = None
    memory: Annotated[
        ReferenceUpdateSelectionInput | SkipJsonSchema[None],
        Field(description="변경할 Memory 참조이며 생략하면 현재 참조를 유지합니다."),
    ] = None
    network: Annotated[
        ReferenceUpdateSelectionInput | SkipJsonSchema[None],
        Field(description="변경할 Network 참조이며 생략하면 현재 참조를 유지합니다."),
    ] = None
    country: Annotated[
        ReferenceUpdateSelectionInput | SkipJsonSchema[None],
        Field(description="변경할 Country 참조이며 생략하면 현재 참조를 유지합니다."),
    ] = None

    @model_validator(mode="after")
    def require_one_non_null_change(self) -> "MasterCodeReferenceUpdatesInput":
        if not self.model_fields_set or any(
            getattr(self, slot) is None for slot in self.model_fields_set
        ):
            raise ValueError("하나 이상의 REFERENCE 또는 NOT_APPLICABLE 자리를 입력해야 합니다.")
        return self


class MasterCodeReferenceUpdateRequest(BaseModel):
    """관리자 직접 MasterCode 참조 수정 입력입니다."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "example": {
                "dimensions": {
                    "brand": {
                        "mode": "REFERENCE",
                        "id": "01a088e9-e4e8-7000-8000-000000000002",
                    },
                    "country": {"mode": "NOT_APPLICABLE"},
                },
                "reason": "잘못 연결된 참조 정정",
            }
        },
    )
    dimensions: Annotated[
        MasterCodeReferenceUpdatesInput,
        Field(description="변경할 자리만 입력하며 생략한 자리는 현재 참조를 유지합니다."),
    ]
    reason: Annotated[
        StrictStr | None,
        Field(
            description="참조 수정 이유이며 앞뒤 일반 공백을 제거한 결과가 500자 이하여야 합니다.",
            json_schema_extra={"x-normalized-maxLength": 500},
        ),
    ] = None


class StringDimensionReferenceResponse(BaseModel):
    """현재 문자열 Dimension 표현입니다."""

    id: Annotated[UUID, Field(description="현재 참조하는 Dimension UUID입니다.")]
    code: Annotated[str, Field(description="합성 코드에 사용한 현재 Dimension 대표 코드입니다.")]
    value: Annotated[str, Field(description="정규화된 현재 문자열 Dimension 값입니다.")]


class NumericDimensionReferenceResponse(BaseModel):
    """현재 Year 또는 Network Dimension 표현입니다."""

    id: Annotated[UUID, Field(description="현재 참조하는 Dimension UUID입니다.")]
    code: Annotated[str, Field(description="합성 코드에 사용한 현재 Dimension 대표 코드입니다.")]
    value: Annotated[int, Field(description="검증된 현재 정수 Dimension 값입니다.")]


class MasterCodeMemoryValueResponse(BaseModel):
    """현재 Memory 값과 파생 용량입니다."""

    amount: Annotated[int, Field(description="저장된 Memory 양입니다.")]
    unit: Annotated[
        Literal["MB", "GB", "TB", "PB"],
        Field(description="정규화된 Memory 단위입니다."),
    ]
    capacity_mb: Annotated[
        int,
        Field(description="십진 SI 배수로 계산한 MB 기준 동등 용량입니다."),
    ]


class MasterCodeMemoryDimensionReferenceResponse(BaseModel):
    """현재 Memory Dimension 표현입니다."""

    id: Annotated[UUID, Field(description="현재 참조하는 Memory Dimension UUID입니다.")]
    code: Annotated[str, Field(description="합성 코드에 사용한 현재 Memory 대표 코드입니다.")]
    value: Annotated[
        MasterCodeMemoryValueResponse,
        Field(description="현재 Memory 값과 파생된 MB 동등 용량입니다."),
    ]


class MasterCodeDimensionsResponse(BaseModel):
    """MasterCode가 참조하는 현재 Dimension 상태입니다."""

    company: Annotated[
        StringDimensionReferenceResponse | None,
        Field(description="현재 Company 참조이며 해당 없음이면 null입니다."),
    ]
    brand: Annotated[
        StringDimensionReferenceResponse | None,
        Field(description="현재 Brand 참조이며 해당 없음이면 null입니다."),
    ]
    model: Annotated[
        StringDimensionReferenceResponse | None,
        Field(description="현재 Model 참조이며 해당 없음이면 null입니다."),
    ]
    category: Annotated[
        StringDimensionReferenceResponse | None,
        Field(description="현재 Category 참조이며 해당 없음이면 null입니다."),
    ]
    year: Annotated[
        NumericDimensionReferenceResponse | None,
        Field(description="현재 Year 참조이며 해당 없음이면 null입니다."),
    ]
    memory: Annotated[
        MasterCodeMemoryDimensionReferenceResponse | None,
        Field(description="현재 Memory 참조이며 해당 없음이면 null입니다."),
    ]
    network: Annotated[
        NumericDimensionReferenceResponse | None,
        Field(description="현재 Network 참조이며 해당 없음이면 null입니다."),
    ]
    country: Annotated[
        StringDimensionReferenceResponse | None,
        Field(description="현재 Country 참조이며 해당 없음이면 null입니다."),
    ]


class MasterCodeResponse(BaseModel):
    """서로 일치하는 한 시점의 활성 MasterCode와 현재 Dimension 표현입니다."""

    model_config = ConfigDict(json_schema_extra={"example": MASTER_CODE_RESPONSE_EXAMPLE})

    id: Annotated[UUID, Field(description="서버가 생성한 MasterCode UUIDv7 식별자입니다.")]
    code: Annotated[
        str,
        Field(
            description=(
                "company, brand, model, category, year, memory, network, country 순서로 현재 "
                "대표 코드를 하이픈으로 연결한 값입니다. 해당 없음인 자리는 NNN입니다."
            )
        ),
    ]
    version: Annotated[int, Field(description="MasterCode 자체 상태의 낙관적 잠금 버전입니다.")]
    created_at: Annotated[datetime, Field(description="MasterCode 생성 시각입니다.")]
    updated_at: Annotated[
        datetime,
        Field(description="MasterCode 자체 상태의 마지막 변경 시각입니다."),
    ]
    deleted_at: Annotated[
        None,
        Field(description="활성 MasterCode만 반환하는 현재 엔드포인트에서는 항상 null입니다."),
    ]
    dimensions: Annotated[
        MasterCodeDimensionsResponse,
        Field(description="MasterCode의 다른 응답 필드와 같은 시점에 유효한 Dimension 표현입니다."),
    ]


class MasterCodeListResponse(BaseModel):
    """최신 생성 순서의 MasterCode cursor 페이지입니다."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "items": [MASTER_CODE_RESPONSE_EXAMPLE],
                "next_cursor": CURSOR_EXAMPLE,
            }
        }
    )

    items: Annotated[
        list[MasterCodeResponse],
        Field(description="현재 페이지의 활성 MasterCode 목록입니다."),
    ]
    next_cursor: Annotated[
        str | None,
        Field(
            max_length=512,
            description="다음 페이지가 있으면 전달되는 불투명 cursor이며 마지막이면 null입니다.",
            examples=[CURSOR_EXAMPLE],
        ),
    ]


def build_master_code_router(
    *,
    create_master_code: CreateMasterCode,
    get_master_code: GetMasterCode,
    list_master_codes: ListMasterCodes,
    update_master_code_references: UpdateMasterCodeReferences,
    principal_dependency: Callable[..., HumanPrincipal],
    authorization: AuthorizationPolicy,
) -> APIRouter:
    """Build MasterCode routes from explicit use-case dependencies."""
    router = APIRouter(prefix=f"{API_V1_PREFIX}/master-codes", tags=["MasterCodes"])
    mutation_guard = build_authorization_guard(
        principal_dependency, authorization, AuthorizationAction.MUTATE_DATA
    )
    read_guard = build_authorization_guard(
        principal_dependency, authorization, AuthorizationAction.READ_DATA
    )

    @router.post(
        "",
        operation_id="create_master_code",
        response_model=MasterCodeResponse,
        status_code=status.HTTP_201_CREATED,
        summary="MasterCode 생성",
        description=(
            "ADMIN 또는 SUPER_ADMIN이 8개 자리를 모두 지정해 MasterCode를 생성합니다. "
            "CREATE로 제안한 Dimension과 MasterCode는 한 요청에서 모두 생성되며, 어느 하나라도 "
            "실패하면 일부만 저장되지 않습니다."
        ),
        responses={
            201: _etag_response("MasterCode를 생성했습니다."),
            401: INVALID_ACCESS_TOKEN_RESPONSE,
            403: AUTHORIZATION_DENIED_RESPONSE,
            409: _conflict_response(),
            422: _create_validation_response(),
            503: _service_unavailable_response(),
        },
    )
    async def create_master_code_route(
        payload: Annotated[
            MasterCodeCreateRequest,
            Body(
                openapi_examples={
                    "mixedSelections": {
                        "summary": "기존 참조, 신규 생성, 해당 없음 혼합",
                        "value": MASTER_CODE_CREATE_EXAMPLE,
                    }
                }
            ),
        ],
        response: Response,
        principal: Annotated[HumanPrincipal, Depends(mutation_guard)],
    ) -> MasterCodeResponse:
        dimensions = _create_input(payload.dimensions)
        try:
            master_code = await create_master_code.execute(
                principal,
                dimensions=dimensions,
                reason=payload.reason,
            )
        except DimensionValidationError as error:
            raise DimensionValidationError(f"body.{error.field}", error.message) from error
        response.headers["ETag"] = master_code.etag()
        return _response(master_code)

    @router.get(
        "",
        operation_id="list_master_codes",
        response_model=MasterCodeListResponse,
        status_code=status.HTTP_200_OK,
        summary="MasterCode 목록 조회",
        description=(
            "활성 MasterCode를 생성 시각과 ID의 내림차순으로 조회합니다. next_cursor를 다음 "
            "요청에 그대로 전달하며 전체 항목 수는 제공하지 않습니다. 페이지마다 limit을 바꿀 "
            "수 있고 cursor는 만료되지 않습니다. 페이지를 순회하는 동안 새로 생긴 항목은 이미 "
            "지난 앞쪽 구간에 위치하므로 뒤 페이지에 끼어들지 않으며, 각 페이지 요청 시점에 이미 "
            "삭제된 항목은 결과에서 제외됩니다."
        ),
        responses={
            200: _list_success_response(),
            401: INVALID_ACCESS_TOKEN_RESPONSE,
            422: _pagination_validation_response(),
            503: _service_unavailable_response(),
        },
    )
    async def list_master_codes_route(
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
            Query(ge=1, le=100, description="페이지 항목 수이며 기본값은 50입니다."),
        ] = 50,
    ) -> MasterCodeListResponse:
        after = None if cursor is None else _decode_cursor(cursor)
        page = await list_master_codes.execute(principal, after=after, limit=limit)
        return _page_response(page)

    @router.patch(
        "/{master_code_id}",
        operation_id="update_master_code_references",
        response_model=MasterCodeResponse,
        status_code=status.HTTP_200_OK,
        summary="MasterCode Dimension 참조 수정",
        description=(
            "ADMIN 또는 SUPER_ADMIN이 직전 단건 응답에서 받은, MasterCode와 현재 참조 "
            "Dimension 상태를 함께 반영한 강한 ETag를 If-Match로 제공해 하나 이상의 Dimension "
            "참조를 교체하거나 NOT_APPLICABLE로 해제합니다. 생략한 자리는 유지하며 최종 참조가 "
            "현재와 같으면 version·시각·감사 로그를 변경하지 않습니다."
        ),
        responses={
            200: _etag_response("수정 후 현재 MasterCode 상태를 반환합니다."),
            400: _master_code_precondition_response(400),
            401: INVALID_ACCESS_TOKEN_RESPONSE,
            403: AUTHORIZATION_DENIED_RESPONSE,
            404: _not_found_response(),
            409: _reference_update_conflict_response(),
            412: _master_code_precondition_response(412),
            422: _reference_update_validation_response(),
            428: _master_code_precondition_response(428),
            503: _service_unavailable_response(
                "/api/v1/master-codes/01890f7c-8abc-7def-8abc-222222222222"
            ),
        },
    )
    async def update_master_code_references_route(
        master_code_id: Annotated[UUID, Path(description="수정할 MasterCode UUID입니다.")],
        payload: MasterCodeReferenceUpdateRequest,
        request: Request,
        response: Response,
        principal: Annotated[HumanPrincipal, Depends(mutation_guard)],
        if_match: MasterCodeIfMatchHeader,
    ) -> MasterCodeResponse:
        del if_match
        expected_etag = _parse_master_code_if_match_values(request.headers.getlist("if-match"))
        changes = _reference_update_input(payload.dimensions)
        try:
            master_code = await update_master_code_references.execute(
                principal,
                master_code_id=master_code_id,
                changes=changes,
                expected_etag=expected_etag,
                reason=payload.reason,
            )
        except DimensionValidationError as error:
            raise DimensionValidationError(f"body.{error.field}", error.message) from error
        response.headers["ETag"] = master_code.etag()
        return _response(master_code)

    @router.get(
        "/{master_code_id}",
        operation_id="get_master_code",
        response_model=MasterCodeResponse,
        status_code=status.HTTP_200_OK,
        summary="MasterCode 단건 조회",
        description=(
            "활성 MasterCode와 8개 Dimension의 현재 상태를 서로 일치하는 한 시점의 표현으로 "
            "반환합니다. MasterCode 자체 또는 포함된 Dimension이 변경되면 달라지는 강한 ETag도 "
            "함께 제공합니다."
        ),
        responses={
            200: _etag_response("활성 MasterCode를 반환합니다."),
            401: INVALID_ACCESS_TOKEN_RESPONSE,
            404: _not_found_response(),
            422: _validation_response(),
            503: _service_unavailable_response(
                "/api/v1/master-codes/01890f7c-8abc-7def-8abc-222222222222"
            ),
        },
    )
    async def get_master_code_route(
        master_code_id: Annotated[UUID, Path(description="조회할 MasterCode UUID입니다.")],
        response: Response,
        principal: Annotated[HumanPrincipal, Depends(read_guard)],
    ) -> MasterCodeResponse:
        master_code = await get_master_code.execute(principal, master_code_id)
        response.headers["ETag"] = master_code.etag()
        return _response(master_code)

    return router


def _create_input(value: MasterCodeDimensionsInput) -> MasterCodeCreateInput:
    return MasterCodeCreateInput(
        **{slot: _selection(getattr(value, slot)) for slot in MasterCodeDimensions.ORDER}
    )


def _reference_update_input(
    value: MasterCodeReferenceUpdatesInput,
) -> MasterCodeReferenceUpdate:
    changes: dict[str, ReferenceSelection] = {
        slot: _reference_selection(getattr(value, slot)) for slot in value.model_fields_set
    }
    return MasterCodeReferenceUpdate(**changes)


def _reference_selection(
    value: ReferenceInput | NotApplicableInput,
) -> ReferenceSelection:
    if isinstance(value, ReferenceInput):
        return ExistingDimension(value.id)
    return NotApplicable()


def _parse_master_code_if_match_values(values: list[str]) -> str:
    if not values:
        raise PreconditionRequired
    if len(values) != 1 or _MASTER_CODE_ETAG.fullmatch(values[0]) is None:
        raise InvalidIfMatch
    return values[0]


def _selection(value: BaseModel) -> ExistingDimension | InlineDimension | NotApplicable:
    if isinstance(value, ReferenceInput):
        return ExistingDimension(value.id)
    if isinstance(value, NotApplicableInput):
        return NotApplicable()
    if isinstance(value, MasterCodeMemoryCreateInput):
        create_value: object = MemoryCreateValue(
            amount=value.value.amount,
            unit=value.value.unit,
        )
    else:
        assert isinstance(value, (StringCreateInput, YearCreateInput, NetworkCreateInput))
        create_value = value.value
    return InlineDimension(code=value.code, value=create_value)


def _response(master_code: MasterCode) -> MasterCodeResponse:
    if master_code.deleted_at is not None:
        raise ValueError("inactive MasterCode cannot be serialized")
    dimensions = {
        slot: _dimension_response(getattr(master_code.dimensions, slot))
        for slot in MasterCodeDimensions.ORDER
    }
    return MasterCodeResponse(
        id=master_code.id,
        code=master_code.code,
        version=master_code.version,
        created_at=master_code.created_at,
        updated_at=master_code.updated_at,
        deleted_at=None,
        dimensions=MasterCodeDimensionsResponse(**dimensions),
    )


def _dimension_response(
    dimension: MasterCodeDimension | None,
) -> (
    StringDimensionReferenceResponse
    | NumericDimensionReferenceResponse
    | MasterCodeMemoryDimensionReferenceResponse
    | None
):
    if dimension is None:
        return None
    common = {"id": dimension.id, "code": dimension.code.value}
    if isinstance(dimension.value, MemoryValue):
        return MasterCodeMemoryDimensionReferenceResponse(
            **common,
            value=MasterCodeMemoryValueResponse(
                amount=dimension.value.amount,
                unit=dimension.value.unit.value,
                capacity_mb=dimension.value.capacity_mb,
            ),
        )
    if isinstance(dimension.value, YearValue):
        return NumericDimensionReferenceResponse(**common, value=dimension.value.value)
    if isinstance(dimension.value, NetworkGeneration):
        return NumericDimensionReferenceResponse(**common, value=dimension.value.value)
    return StringDimensionReferenceResponse(**common, value=dimension.value.value)


def _page_response(page: MasterCodePage) -> MasterCodeListResponse:
    next_cursor = None
    if page.has_more and page.items:
        last = page.items[-1]
        next_cursor = _encode_cursor(MasterCodeCursor(created_at=last.created_at, id=last.id))
    return MasterCodeListResponse(
        items=[_response(master_code) for master_code in page.items],
        next_cursor=next_cursor,
    )


def _encode_cursor(cursor: MasterCodeCursor) -> str:
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


def _decode_cursor(encoded: str) -> MasterCodeCursor:
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
        return MasterCodeCursor(created_at=timestamp, id=UUID(payload["i"]))
    except (UnicodeError, ValueError, TypeError, json.JSONDecodeError):
        raise DimensionValidationError("query.cursor", "유효하지 않은 cursor입니다.") from None


def _etag_response(description: str) -> dict[str, Any]:
    return {
        "description": description,
        "content": {
            "application/json": {
                "schema": {"$ref": "#/components/schemas/MasterCodeResponse"},
                "example": MASTER_CODE_RESPONSE_EXAMPLE,
            }
        },
        "headers": {
            "ETag": {
                "description": (
                    "MasterCode와 여덟 자리의 현재 Dimension 상태를 함께 반영한 강한 ETag입니다."
                ),
                "schema": {
                    "type": "string",
                    "example": ETAG_EXAMPLE,
                },
            }
        },
    }


def _list_success_response() -> dict[str, Any]:
    return {
        "description": "활성 MasterCode 목록을 반환합니다.",
        "content": {
            "application/json": {
                "schema": {"$ref": "#/components/schemas/MasterCodeListResponse"},
                "example": {
                    "items": [MASTER_CODE_RESPONSE_EXAMPLE],
                    "next_cursor": CURSOR_EXAMPLE,
                },
            }
        },
    }


def _problem_response(
    *,
    description: str,
    example: dict[str, Any] | None = None,
    examples: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    media: dict[str, Any] = {"schema": {"$ref": "#/components/schemas/ProblemDetails"}}
    if example is not None:
        media["example"] = example
    if examples is not None:
        media["examples"] = {
            name: {
                "summary": value["summary"],
                "value": {key: item for key, item in value.items() if key != "summary"},
            }
            for name, value in examples.items()
        }
    return {
        "model": ProblemDetails,
        "description": description,
        "content": {"application/problem+json": media},
    }


def _validation_response() -> dict[str, Any]:
    return _problem_response(
        description="요청 필드가 유효하지 않습니다.",
        example={
            "type": "/problems/validation-error",
            "title": "유효하지 않은 요청",
            "status": 422,
            "detail": "하나 이상의 요청 필드가 유효하지 않습니다.",
            "code": "VALIDATION_ERROR",
            "instance": "/api/v1/master-codes/not-a-uuid",
            "violations": [
                {"field": "path.master_code_id", "message": "유효한 UUID를 입력해야 합니다."}
            ],
        },
    )


def _create_validation_response() -> dict[str, Any]:
    return _problem_response(
        description="요청 필드 또는 기존 Dimension 참조가 유효하지 않습니다.",
        examples={
            "validationError": {
                "summary": "필수 자리 누락",
                "type": "/problems/validation-error",
                "title": "유효하지 않은 요청",
                "status": 422,
                "detail": "하나 이상의 요청 필드가 유효하지 않습니다.",
                "code": "VALIDATION_ERROR",
                "instance": "/api/v1/master-codes",
                "violations": [{"field": "body.dimensions.country", "message": "필수 필드입니다."}],
            },
            "invalidDimensionReference": {
                "summary": "없거나 비활성인 참조",
                "type": "/problems/invalid-dimension-reference",
                "title": "유효하지 않은 Dimension 참조",
                "status": 422,
                "detail": "하나 이상의 Dimension 참조가 없거나 활성 상태가 아닙니다.",
                "code": "INVALID_DIMENSION_REFERENCE",
                "instance": "/api/v1/master-codes",
                "violations": [
                    {
                        "field": "body.dimensions.company.id",
                        "message": "활성 Dimension을 참조해야 합니다.",
                    }
                ],
            },
        },
    )


def _reference_update_validation_response() -> dict[str, Any]:
    return _problem_response(
        description="참조 수정 입력 또는 기존 Dimension 참조가 유효하지 않습니다.",
        examples={
            "emptyChanges": {
                "summary": "변경 자리 없음",
                "type": "/problems/validation-error",
                "title": "유효하지 않은 요청",
                "status": 422,
                "detail": "하나 이상의 요청 필드가 유효하지 않습니다.",
                "code": "VALIDATION_ERROR",
                "instance": ("/api/v1/master-codes/01890f7c-8abc-7def-8abc-222222222222"),
                "violations": [
                    {"field": "body.dimensions", "message": "하나 이상 변경해야 합니다."}
                ],
            },
            "invalidDimensionReference": {
                "summary": "없거나 비활성인 참조",
                "type": "/problems/invalid-dimension-reference",
                "title": "유효하지 않은 Dimension 참조",
                "status": 422,
                "detail": "하나 이상의 Dimension 참조가 없거나 활성 상태가 아닙니다.",
                "code": "INVALID_DIMENSION_REFERENCE",
                "instance": ("/api/v1/master-codes/01890f7c-8abc-7def-8abc-222222222222"),
                "violations": [
                    {
                        "field": "body.dimensions.company.id",
                        "message": "활성 Dimension을 참조해야 합니다.",
                    }
                ],
            },
        },
    )


def _master_code_precondition_response(status_code: int) -> dict[str, Any]:
    responses = {
        400: (
            "If-Match 헤더 형식이 유효하지 않습니다.",
            "invalid-if-match",
            "유효하지 않은 If-Match",
            "If-Match에는 직전 MasterCode 응답에서 받은, 문서에 제시된 형식의 강한 ETag "
            "하나를 입력해야 합니다.",
            "INVALID_IF_MATCH",
        ),
        412: (
            "If-Match가 현재 MasterCode 표현의 ETag와 일치하지 않습니다.",
            "precondition-failed",
            "사전 조건 불일치",
            "MasterCode 또는 참조 Dimension이 조회 이후 변경되었습니다. 최신 상태와 ETag를 "
            "다시 조회해 주세요.",
            "PRECONDITION_FAILED",
        ),
        428: (
            "조건부 참조 수정에 필요한 If-Match 헤더가 없습니다.",
            "precondition-required",
            "사전 조건 필요",
            "MasterCode를 변경하려면 직전 단건 응답의 ETag를 If-Match로 제공해야 합니다.",
            "PRECONDITION_REQUIRED",
        ),
    }
    description, slug, title, detail, code = responses[status_code]
    return _problem_response(
        description=description,
        example={
            "type": f"/problems/{slug}",
            "title": title,
            "status": status_code,
            "detail": detail,
            "code": code,
            "instance": "/api/v1/master-codes/01890f7c-8abc-7def-8abc-222222222222",
        },
    )


def _pagination_validation_response() -> dict[str, Any]:
    return _problem_response(
        description="cursor 또는 limit이 유효하지 않습니다.",
        example={
            "type": "/problems/validation-error",
            "title": "유효하지 않은 요청",
            "status": 422,
            "detail": "하나 이상의 요청 필드가 유효하지 않습니다.",
            "code": "VALIDATION_ERROR",
            "instance": "/api/v1/master-codes",
            "violations": [{"field": "query.cursor", "message": "유효하지 않은 cursor입니다."}],
        },
    )


def _not_found_response() -> dict[str, Any]:
    return _problem_response(
        description="활성 MasterCode를 찾을 수 없습니다.",
        example={
            "type": "/problems/master-code-not-found",
            "title": "MasterCode를 찾을 수 없음",
            "status": 404,
            "detail": "요청한 활성 MasterCode를 찾을 수 없습니다.",
            "code": "MASTER_CODE_NOT_FOUND",
            "instance": "/api/v1/master-codes/01890f7c-8abc-7def-8abc-222222222222",
        },
    )


def _conflict_response() -> dict[str, Any]:
    return _problem_response(
        description="Dimension 또는 MasterCode 유일성 제약과 충돌합니다.",
        examples={
            "masterCodeConflict": {
                "summary": "MasterCode 중복",
                "type": "/problems/master-code-conflict",
                "title": "MasterCode 충돌",
                "status": 409,
                "detail": "같은 참조 조합 또는 합성 코드의 MasterCode가 이미 존재합니다.",
                "code": "MASTER_CODE_CONFLICT",
                "instance": "/api/v1/master-codes",
            },
            "dimensionCodeConflict": {
                "summary": "mode: CREATE Dimension 대표 코드 중복",
                "type": "/problems/dimension-code-conflict",
                "title": "Dimension 대표 코드 충돌",
                "status": 409,
                "detail": "mode: CREATE로 생성하려는 Dimension의 대표 코드가 이미 사용 중입니다.",
                "code": "DIMENSION_CODE_CONFLICT",
                "instance": "/api/v1/master-codes",
                "violations": [
                    {
                        "field": "body.dimensions.company.code",
                        "message": "이미 사용 중인 값입니다.",
                    }
                ],
            },
            "dimensionValueConflict": {
                "summary": "mode: CREATE Dimension 값 중복",
                "type": "/problems/dimension-value-conflict",
                "title": "Dimension 값 충돌",
                "status": 409,
                "detail": "mode: CREATE로 생성하려는 Dimension의 값이 이미 사용 중입니다.",
                "code": "DIMENSION_VALUE_CONFLICT",
                "instance": "/api/v1/master-codes",
                "violations": [
                    {
                        "field": "body.dimensions.company.value",
                        "message": "이미 사용 중인 값입니다.",
                    }
                ],
            },
            "dimensionMultipleConflicts": {
                "summary": "mode: CREATE Dimension 대표 코드와 값 중복",
                "type": "/problems/dimension-multiple-conflicts",
                "title": "여러 Dimension 필드 충돌",
                "status": 409,
                "detail": (
                    "mode: CREATE로 생성하려는 Dimension의 대표 코드와 값이 모두 "
                    "이미 사용 중입니다."
                ),
                "code": "DIMENSION_MULTIPLE_CONFLICTS",
                "instance": "/api/v1/master-codes",
                "violations": [
                    {
                        "field": "body.dimensions.company.code",
                        "message": "이미 사용 중인 값입니다.",
                    },
                    {
                        "field": "body.dimensions.company.value",
                        "message": "이미 사용 중인 값입니다.",
                    },
                ],
            },
        },
    )


def _reference_update_conflict_response() -> dict[str, Any]:
    return _problem_response(
        description="변경 후 참조 조합 또는 합성 코드가 기존 MasterCode와 충돌합니다.",
        example={
            "type": "/problems/master-code-conflict",
            "title": "MasterCode 충돌",
            "status": 409,
            "detail": "같은 참조 조합 또는 합성 코드의 MasterCode가 이미 존재합니다.",
            "code": "MASTER_CODE_CONFLICT",
            "instance": "/api/v1/master-codes/01890f7c-8abc-7def-8abc-222222222222",
        },
    )


def _service_unavailable_response(
    instance: str = "/api/v1/master-codes",
) -> dict[str, Any]:
    return _problem_response(
        description="데이터를 일시적으로 처리할 수 없습니다.",
        example={
            "type": "/problems/service-unavailable",
            "title": "서비스를 사용할 수 없음",
            "status": 503,
            "detail": "요청을 일시적으로 처리할 수 없습니다. 잠시 후 다시 시도해 주세요.",
            "code": "SERVICE_UNAVAILABLE",
            "instance": instance,
        },
    )
