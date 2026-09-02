"""Public request and response contracts."""

from typing import Annotated, Literal

from pydantic import AnyUrl, BaseModel, ConfigDict, Field


class HealthResponse(BaseModel):
    """Current availability state reported by a health endpoint."""

    model_config = ConfigDict(
        json_schema_extra={"example": {"status": "ok", "message": "The service is ready."}}
    )

    status: Annotated[
        Literal["ok"],
        Field(description="Short machine-readable health state.", examples=["ok"]),
    ]
    message: Annotated[
        str,
        Field(
            description="Human-readable explanation of the current health state.",
            examples=["The service is ready."],
        ),
    ]


class FieldViolation(BaseModel):
    """One invalid input field reported to an API consumer."""

    field: Annotated[
        str,
        Field(description="Location of the invalid input.", examples=["body.name"]),
    ]
    message: Annotated[
        str,
        Field(description="User-facing explanation of the validation failure."),
    ]


class ProblemDetails(BaseModel):
    """RFC 9457 problem details extended with a stable service error code."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "type": "https://api.example.com/problems/service-unavailable",
                "title": "Service unavailable",
                "status": 503,
                "detail": "A required dependency is unavailable.",
                "code": "SERVICE_UNAVAILABLE",
                "instance": "/health/ready",
            }
        }
    )

    type: Annotated[
        AnyUrl,
        Field(description="Stable URI identifying the category of problem."),
    ]
    title: Annotated[
        str,
        Field(description="Short, human-readable problem summary."),
    ]
    status: Annotated[
        int,
        Field(description="HTTP status code returned for this occurrence.", examples=[503]),
    ]
    detail: Annotated[
        str,
        Field(description="Human-readable explanation specific to this occurrence."),
    ]
    code: Annotated[
        str,
        Field(description="Stable machine-readable service error code."),
    ]
    instance: Annotated[
        str | None,
        Field(description="Request path associated with this occurrence."),
    ] = None
    violations: Annotated[
        list[FieldViolation] | None,
        Field(description="Invalid input fields when the problem is a validation failure."),
    ] = None
