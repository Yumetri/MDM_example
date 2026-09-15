"""Typed public audit snapshots; values retain their original JSON types."""

from datetime import datetime
from typing import Annotated, Literal, cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr

from mdm.domain.audit import ActorKind, MasterCodeOperation
from mdm.domain.auth import UserRole
from mdm.domain.dimensions import MemoryUnit


class AuditResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="forbid")
    id: Annotated[UUID, Field(description="이 로그의 UUID입니다.")]
    change_set_id: Annotated[
        UUID, Field(description="한 변경 묶음의 UUID이며 통합 로그 조회에 사용합니다.")
    ]
    changed_at: Annotated[
        datetime, Field(description="변경 시각이며 UTC로 반환합니다. 커밋 시각과 다를 수 있습니다.")
    ]
    actor_kind: Annotated[
        ActorKind,
        Field(
            description=(
                "변경 수행자의 종류입니다. 현재 제공되는 변경 기능은 HUMAN으로 기록됩니다."
            )
        ),
    ]
    actor_id: Annotated[
        str, Field(min_length=1, max_length=255, description="변경을 수행한 주체의 식별자입니다.")
    ]
    actor_role: Annotated[
        UserRole | None,
        Field(description="변경 당시 사람의 역할이며 SYSTEM 기록에서는 null입니다."),
    ]
    reason: Annotated[
        str | None,
        Field(max_length=500, description="변경 당시 기록한 사유이며 생략한 경우 null입니다."),
    ]


class MemoryAuditValue(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="forbid")
    amount: Annotated[
        StrictInt, Field(ge=1, le=2147483647, description="기록 당시의 Memory 수량입니다.")
    ]
    unit: Annotated[MemoryUnit, Field(description="기록 당시의 십진 SI 용량 단위입니다.")]
    capacity_mb: Annotated[StrictInt, Field(ge=1, description="기록 당시의 MB 환산 용량입니다.")]


class DimensionAuditBase[SourceT](AuditResponse):
    source_kind: Annotated[
        Literal["DIMENSION"], Field(description="Dimension 변경 로그임을 나타냅니다.")
    ]
    source_type: Annotated[SourceT, Field(description="로그가 속한 Dimension 타입입니다.")]
    dimension_id: Annotated[UUID, Field(description="변경된 Dimension의 UUID입니다.")]
    dimension_version: Annotated[
        int, Field(ge=1, description="변경 완료 후 Dimension version입니다.")
    ]


class DimensionCodeLog[SourceT](DimensionAuditBase[SourceT]):
    operation: Annotated[
        Literal["CREATE", "UPDATE"], Field(description="code 생성 또는 변경 작업입니다.")
    ]
    field_name: Annotated[Literal["CODE"], Field(description="code의 변경을 기록합니다.")]
    old_value: Annotated[
        StrictStr | None, Field(description="변경 전 code이며 생성 시 null입니다.")
    ]
    new_value: Annotated[StrictStr, Field(description="변경 후 code입니다.")]


class DimensionValueLog[SourceT, ValueT](DimensionAuditBase[SourceT]):
    operation: Annotated[
        Literal["CREATE", "UPDATE"], Field(description="value 생성 또는 변경 작업입니다.")
    ]
    field_name: Annotated[Literal["VALUE"], Field(description="타입별 value의 변경을 기록합니다.")]
    old_value: Annotated[ValueT | None, Field(description="변경 전 value이며 생성 시 null입니다.")]
    new_value: Annotated[ValueT, Field(description="원래 자료형을 보존한 변경 후 value입니다.")]


class DimensionDeletedLog[SourceT](DimensionAuditBase[SourceT]):
    operation: Annotated[
        Literal["DELETE", "RESTORE"], Field(description="논리 삭제 또는 복원 작업입니다.")
    ]
    field_name: Annotated[
        Literal["DELETED"], Field(description="논리 삭제 여부의 변경을 기록합니다.")
    ]
    old_value: Annotated[StrictBool, Field(description="변경 전 논리 삭제 여부입니다.")]
    new_value: Annotated[StrictBool, Field(description="변경 후 논리 삭제 여부입니다.")]


type DimensionResponse[SourceT, ValueT] = Annotated[
    DimensionCodeLog[SourceT] | DimensionValueLog[SourceT, ValueT] | DimensionDeletedLog[SourceT],
    Field(discriminator="field_name"),
]
type StringValue = Annotated[
    StrictStr, Field(min_length=1, max_length=128, pattern=r"^[A-Z0-9]+(?:_[A-Z0-9]+)*$")
]
type YearValue = Annotated[StrictInt, Field(ge=2000, le=2999)]
type NetworkValue = Annotated[StrictInt, Field(ge=1, le=5)]
type CompanyLogResponse = DimensionResponse[Literal["COMPANY"], StringValue]
type ModelLogResponse = DimensionResponse[Literal["MODEL"], StringValue]
type BrandLogResponse = DimensionResponse[Literal["BRAND"], StringValue]
type CountryLogResponse = DimensionResponse[Literal["COUNTRY"], StringValue]
type CategoryLogResponse = DimensionResponse[Literal["CATEGORY"], StringValue]
type YearLogResponse = DimensionResponse[Literal["YEAR"], YearValue]
type NetworkLogResponse = DimensionResponse[Literal["NETWORK"], NetworkValue]
type MemoryLogResponse = DimensionResponse[Literal["MEMORY"], MemoryAuditValue]
type DimensionLogResponse = Annotated[
    CompanyLogResponse
    | ModelLogResponse
    | BrandLogResponse
    | CountryLogResponse
    | CategoryLogResponse
    | YearLogResponse
    | NetworkLogResponse
    | MemoryLogResponse,
    Field(discriminator="source_type"),
]


class MasterCodeAuditState(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="forbid")
    company_id: Annotated[
        UUID | None, Field(description="당시 Company 참조이며 해당 없으면 null입니다.")
    ]
    brand_id: Annotated[
        UUID | None, Field(description="당시 Brand 참조이며 해당 없으면 null입니다.")
    ]
    model_id: Annotated[
        UUID | None, Field(description="당시 Model 참조이며 해당 없으면 null입니다.")
    ]
    category_id: Annotated[
        UUID | None, Field(description="당시 Category 참조이며 해당 없으면 null입니다.")
    ]
    year_id: Annotated[UUID | None, Field(description="당시 Year 참조이며 해당 없으면 null입니다.")]
    memory_id: Annotated[
        UUID | None, Field(description="당시 Memory 참조이며 해당 없으면 null입니다.")
    ]
    network_id: Annotated[
        UUID | None, Field(description="당시 Network 참조이며 해당 없으면 null입니다.")
    ]
    country_id: Annotated[
        UUID | None, Field(description="당시 Country 참조이며 해당 없으면 null입니다.")
    ]
    code: Annotated[StrictStr, Field(description="기록 당시의 합성 code입니다.")]
    deleted: Annotated[StrictBool, Field(description="기록 당시의 논리 삭제 여부입니다.")]


class MasterCodeLogResponse(AuditResponse):
    source_kind: Annotated[
        Literal["MASTER_CODE"], Field(description="MasterCode 변경 로그임을 나타냅니다.")
    ]
    source_type: Annotated[
        Literal["MASTER_CODE"], Field(description="로그 대상은 MasterCode입니다.")
    ]
    master_code_id: Annotated[UUID, Field(description="변경된 MasterCode의 UUID입니다.")]
    master_code_version: Annotated[
        int, Field(ge=1, description="변경 완료 후 MasterCode version입니다.")
    ]
    operation: Annotated[
        MasterCodeOperation,
        Field(description="생성, 참조 변경, 재합성, 삭제 또는 복원 작업입니다."),
    ]
    old_state: Annotated[
        MasterCodeAuditState | None,
        Field(description="변경 전 참조·code·삭제 상태이며 생성 시 null입니다."),
    ]
    new_state: Annotated[
        MasterCodeAuditState, Field(description="변경 후 참조·code·삭제 상태입니다.")
    ]


type AuditLogResponse = Annotated[
    DimensionLogResponse | MasterCodeLogResponse, Field(discriminator="source_kind")
]


class AuditLogListResponse[EntryT](BaseModel):
    items: Annotated[list[EntryT], Field(description="현재 페이지의 감사 로그 목록입니다.")]
    next_cursor: Annotated[
        str | None,
        Field(
            max_length=512,
            description=(
                "다음 페이지의 cursor입니다. 같은 경로·필터로 사용하며 마지막이면 null입니다."
            ),
        ),
    ]


def audit_log_success_examples(source: str | None) -> dict[str, object]:
    """Keep complete JSON examples, including required nulls, in generated OpenAPI."""
    common: dict[str, object] = {
        "id": "01a088e9-e4e8-7000-8000-000000000003",
        "change_set_id": "01a088e9-e4e8-7000-8000-000000000001",
        "changed_at": "2026-09-15T00:00:00Z",
        "actor_kind": "HUMAN",
        "actor_id": "01a088e9-e4e8-7000-8000-000000000002",
        "actor_role": "ADMIN",
        "reason": "신규 항목 등록",
        "operation": "CREATE",
    }
    master = dict(
        common,
        source_kind="MASTER_CODE",
        source_type="MASTER_CODE",
        master_code_id="01a088e9-e4e8-7000-8000-000000000004",
        master_code_version=1,
        old_state=None,
        new_state={
            "company_id": None,
            "brand_id": None,
            "model_id": None,
            "category_id": None,
            "year_id": None,
            "memory_id": None,
            "network_id": None,
            "country_id": None,
            "code": "NNN-NNN-NNN-NNN-NNN-NNN-NNN-NNN",
            "deleted": False,
        },
    )
    if source == "MASTER_CODE":
        entries = [master]
    else:
        kind = source or "COMPANY"
        value: object = "ACME"
        if kind == "YEAR":
            value = 2026
        elif kind == "NETWORK":
            value = 5
        elif kind == "MEMORY":
            value = {"amount": 128, "unit": "GB", "capacity_mb": 128000}
        dimension = dict(
            common,
            source_kind="DIMENSION",
            source_type=kind,
            dimension_id="01a088e9-e4e8-7000-8000-000000000005",
            dimension_version=1,
            field_name="VALUE",
            old_value=None,
            new_value=value,
        )
        code = dict(
            dimension,
            id="01a088e9-e4e8-7000-8000-000000000006",
            field_name="CODE",
            new_value="ACME",
        )
        entries = [code, dimension]
        if source is None:
            master["id"] = "01a088e9-e4e8-7000-8000-000000000007"
            state = dict(cast(dict[str, object], master["new_state"]))
            state.update(
                company_id=dimension["dimension_id"], code="ACME-NNN-NNN-NNN-NNN-NNN-NNN-NNN"
            )
            master["new_state"] = state
            entries.append(master)
    return {
        "created": {
            "summary": "생성 작업의 로그와 마지막 페이지",
            "value": {"items": entries, "next_cursor": None},
        },
        "empty": {"summary": "검색 결과가 없음", "value": {"items": [], "next_cursor": None}},
    }
