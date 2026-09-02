"""Public request and response contracts."""

from typing import Annotated, Literal

from pydantic import AnyUrl, BaseModel, ConfigDict, Field


class HealthResponse(BaseModel):
    """상태 확인 엔드포인트가 반환하는 현재 서비스 가용성입니다."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "status": "ok",
                "message": "서비스가 요청을 처리할 준비가 되었습니다.",
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
    """API 사용자에게 반환하는 유효하지 않은 입력 필드 하나의 정보입니다."""

    field: Annotated[
        str,
        Field(description="유효하지 않은 입력값의 위치입니다.", examples=["body.name"]),
    ]
    message: Annotated[
        str,
        Field(description="입력값 검증 실패 원인을 설명하는 사용자용 메시지입니다."),
    ]


class ProblemDetails(BaseModel):
    """안정적인 서비스 오류 코드를 추가한 RFC 9457 Problem Details입니다."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "type": "https://api.example.com/problems/service-unavailable",
                "title": "서비스를 사용할 수 없음",
                "status": 503,
                "detail": "필수 의존 서비스를 사용할 수 없습니다.",
                "code": "SERVICE_UNAVAILABLE",
                "instance": "/health/ready",
            }
        }
    )

    type: Annotated[
        AnyUrl,
        Field(description="문제 유형을 식별하는 안정적인 URI입니다."),
    ]
    title: Annotated[
        str,
        Field(description="문제를 짧게 요약한 사용자용 문구입니다."),
    ]
    status: Annotated[
        int,
        Field(description="해당 문제 발생에 대해 반환한 HTTP 상태 코드입니다.", examples=[503]),
    ]
    detail: Annotated[
        str,
        Field(description="해당 문제 발생의 구체적인 원인을 설명하는 사용자용 문구입니다."),
    ]
    code: Annotated[
        str,
        Field(description="안정적이고 기계 판독 가능한 서비스 오류 코드입니다."),
    ]
    instance: Annotated[
        str | None,
        Field(description="해당 문제 발생 건을 식별하는 요청 경로 형식의 URI 참조입니다."),
    ] = None
    violations: Annotated[
        list[FieldViolation] | None,
        Field(description="입력값 검증 실패 시 유효하지 않은 필드 목록입니다."),
    ] = None
