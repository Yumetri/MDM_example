"""HTTP parsing and documentation for Dimension version preconditions."""

import re
from collections.abc import Sequence
from typing import Annotated, Any

from fastapi import Header

from mdm.api.schemas import ProblemDetails
from mdm.application.preconditions import InvalidIfMatch, PreconditionRequired

_DIMENSION_ETAG = re.compile(r'^"([1-9][0-9]*)"$', re.ASCII)

DimensionIfMatchHeader = Annotated[
    str,
    Header(
        alias="If-Match",
        description=(
            "직전 단건 응답에서 받은 정수 version의 강한 ETag입니다. 따옴표를 포함한 "
            "정확히 하나의 값만 허용합니다."
        ),
        examples=['"1"'],
        json_schema_extra={
            "pattern": r'^"[1-9][0-9]*"$',
            "x-version-maximum": 2_147_483_647,
        },
    ),
]


def parse_dimension_if_match(value: str | None) -> int:
    """Parse exactly one canonical strong ETag containing a positive version."""
    if value is None:
        raise PreconditionRequired
    match = _DIMENSION_ETAG.fullmatch(value)
    if match is None:
        raise InvalidIfMatch
    digits = match.group(1)
    if len(digits) > 10:
        raise InvalidIfMatch
    version = int(digits)
    if version > 2_147_483_647:
        raise InvalidIfMatch
    return version


def parse_dimension_if_match_values(values: Sequence[str]) -> int:
    """Reject repeated field lines before parsing the single If-Match value."""
    if not values:
        raise PreconditionRequired
    if len(values) != 1:
        raise InvalidIfMatch
    return parse_dimension_if_match(values[0])


def dimension_precondition_responses() -> dict[int, dict[str, Any]]:
    """Return reusable RFC 9457 documentation for conditional mutations."""
    return {
        400: _response(
            "If-Match 헤더 형식이 유효하지 않습니다.",
            "invalid-if-match",
            "유효하지 않은 If-Match",
            400,
            "If-Match에는 따옴표로 감싼 강한 정수 ETag 하나를 입력해야 합니다.",
            "INVALID_IF_MATCH",
        ),
        412: _response(
            "If-Match가 현재 Dimension version과 일치하지 않습니다.",
            "precondition-failed",
            "사전 조건 불일치",
            412,
            "Dimension이 조회 이후 변경되었습니다. 최신 상태와 ETag를 다시 조회해 주세요.",
            "PRECONDITION_FAILED",
        ),
        428: _response(
            "조건부 변경에 필요한 If-Match 헤더가 없습니다.",
            "precondition-required",
            "사전 조건 필요",
            428,
            "Dimension을 변경하려면 직전 단건 응답의 ETag를 If-Match로 제공해야 합니다.",
            "PRECONDITION_REQUIRED",
        ),
    }


def _response(
    description: str,
    slug: str,
    title: str,
    status: int,
    detail: str,
    code: str,
) -> dict[str, Any]:
    return {
        "model": ProblemDetails,
        "description": description,
        "content": {
            "application/problem+json": {
                "schema": {"$ref": "#/components/schemas/ProblemDetails"},
                "example": {
                    "type": f"/problems/{slug}",
                    "title": title,
                    "status": status,
                    "detail": detail,
                    "code": code,
                },
            }
        },
    }
