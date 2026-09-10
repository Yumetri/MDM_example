"""Public request and response contracts."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic.json_schema import SkipJsonSchema


class HealthResponse(BaseModel):
    """상태 확인 엔드포인트가 반환하는 결과입니다."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "status": "ok",
                "message": "상태 확인이 성공했습니다.",
            }
        }
    )

    status: Annotated[
        Literal["ok"],
        Field(
            description="상태 확인 결과를 나타내는 짧고 기계 판독 가능한 값입니다.",
            examples=["ok"],
        ),
    ]
    message: Annotated[
        str,
        Field(
            description="현재 서비스 상태를 설명하는 사용자용 메시지입니다.",
            examples=["서비스가 요청을 처리할 준비가 되었습니다."],
        ),
    ]


class FieldViolation(BaseModel):
    """입력값 검증에 실패한 필드 하나에 대한 정보입니다."""

    field: Annotated[
        str,
        Field(description="유효하지 않은 입력값의 위치입니다.", examples=["body.code"]),
    ]
    message: Annotated[
        str,
        Field(
            description="입력값 검증 실패 원인을 설명하는 사용자용 메시지입니다.",
            examples=["필수 필드입니다."],
        ),
    ]


class ProblemDetails(BaseModel):
    """안정적인 서비스 오류 코드를 추가한 RFC 9457 Problem Details입니다."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "type": "/problems/service-unavailable",
                "title": "서비스를 사용할 수 없음",
                "status": 503,
                "detail": "필수 의존 서비스를 사용할 수 없습니다.",
                "code": "SERVICE_UNAVAILABLE",
                "instance": "/api/v1/health/ready",
            }
        }
    )

    type: Annotated[
        str,
        Field(
            pattern=r"^/problems/[a-z0-9]+(?:-[a-z0-9]+)*$",
            description=(
                "문제 유형을 식별하는 안정적인 상대 URI입니다. 현재 API origin을 기준으로 "
                "해석하며, code는 서비스 전용 보조 식별자입니다."
            ),
            examples=["/problems/service-unavailable"],
            json_schema_extra={"format": "uri-reference"},
        ),
    ]
    title: Annotated[
        str,
        Field(
            description="문제를 짧게 요약한 사용자용 문구입니다.",
            examples=["서비스를 사용할 수 없음"],
        ),
    ]
    status: Annotated[
        int,
        Field(description="해당 문제 발생에 대해 반환한 HTTP 상태 코드입니다.", examples=[503]),
    ]
    detail: Annotated[
        str,
        Field(
            description="해당 문제 발생의 구체적인 원인을 설명하는 사용자용 문구입니다.",
            examples=["필수 의존 서비스를 사용할 수 없습니다."],
        ),
    ]
    code: Annotated[
        str,
        Field(
            description="안정적이고 기계 판독 가능한 서비스 오류 코드입니다.",
            examples=["SERVICE_UNAVAILABLE"],
        ),
    ]
    instance: Annotated[
        str | SkipJsonSchema[None],
        Field(
            description="이 문제 발생을 식별하는 URI 참조입니다.",
            examples=["/api/v1/health/ready"],
            json_schema_extra={"format": "uri-reference"},
        ),
    ] = None
    violations: Annotated[
        list[FieldViolation] | None,
        Field(
            description="입력값 검증 또는 충돌과 관련된 필드별 문제 목록입니다.",
            examples=[[{"field": "body.code", "message": "필수 필드입니다."}]],
        ),
    ] = None
