"""Administrator-only audit log endpoints and query-bound opaque cursors."""

import base64
import hashlib
import json
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Path, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field

from mdm.api.audit_log_schemas import (
    AuditLogListResponse,
    AuditLogResponse,
    BrandLogResponse,
    CategoryLogResponse,
    CompanyLogResponse,
    CountryLogResponse,
    MasterCodeLogResponse,
    MemoryLogResponse,
    ModelLogResponse,
    NetworkLogResponse,
    YearLogResponse,
)
from mdm.api.auth import INVALID_ACCESS_TOKEN_RESPONSE
from mdm.api.authorization import AUTHORIZATION_DENIED_RESPONSE, build_authorization_guard
from mdm.api.errors import problem_response
from mdm.api.routes import API_V1_PREFIX
from mdm.api.schemas import ProblemDetails
from mdm.application.audit_logs import (
    AuditField,
    AuditLogCursor,
    AuditLogFilters,
    AuditLogPage,
    AuditLogQuery,
    AuditSource,
    ListAuditLogs,
)
from mdm.application.auth import HumanPrincipal
from mdm.application.authorization import AuthorizationAction, AuthorizationPolicy
from mdm.domain.audit import ActorKind, DimensionOperation, MasterCodeOperation
from mdm.domain.auth import UserRole
from mdm.domain.dimensions import DimensionValidationError


def _timestamp(value: object) -> datetime:
    if not isinstance(value, str) or not any(separator in value for separator in ("T", "t", " ")):
        raise ValueError("시간대가 포함된 날짜·시각 문자열을 입력해야 합니다.")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError("유효한 날짜·시각을 입력해야 합니다.") from None
    if parsed.utcoffset() is None:
        raise ValueError("시간대를 포함해야 합니다.")
    try:
        return parsed.astimezone(UTC)
    except OverflowError:
        raise ValueError("UTC로 표현할 수 있는 날짜·시각을 입력해야 합니다.") from None


type QueryTimestamp = Annotated[datetime, BeforeValidator(_timestamp)]


class PageParameters(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cursor: Annotated[
        str | None,
        Field(
            min_length=1,
            max_length=512,
            description="직전 next_cursor입니다. 경로·필터를 바꾸면 생략하고 다시 조회합니다.",
        ),
    ] = None
    limit: Annotated[
        int, Field(ge=1, le=100, description="페이지 항목 수이며 기본 50개, 최대 100개입니다.")
    ] = 50


class LogParameters(PageParameters):
    change_set_id: Annotated[
        UUID | None, Field(description="해당 변경 묶음의 UUID와 일치하는 로그만 조회합니다.")
    ] = None
    changed_from: Annotated[
        QueryTimestamp | None,
        Field(
            description=(
                "이 시각 이상인 변경을 조회합니다. 시간대는 필수이며 생략 시 하한이 없습니다."
            )
        ),
    ] = None
    changed_before: Annotated[
        QueryTimestamp | None,
        Field(
            description=(
                "시간대가 있는 종료 시각 미만을 조회합니다. "
                "시작보다 늦어야 하며 생략 시 상한이 없습니다."
            )
        ),
    ] = None
    actor_id: Annotated[
        str | None,
        Field(
            min_length=1,
            max_length=255,
            description="기록된 작업 수행자의 식별자와 정확히 일치하는 로그만 조회합니다.",
        ),
    ] = None
    actor_kind: Annotated[
        ActorKind | None,
        Field(description="기록된 작업 수행자의 종류와 일치하는 로그만 조회합니다."),
    ] = None
    actor_role: Annotated[
        UserRole | None,
        Field(description="사용자의 현재 역할이 아닌 변경 당시 기록된 역할로 조회합니다."),
    ] = None

    def filters(
        self,
        *,
        entity_id: UUID | None,
        operation: DimensionOperation | MasterCodeOperation | None,
        field_name: AuditField | None = None,
    ) -> AuditLogFilters:
        return AuditLogFilters(
            entity_id=entity_id,
            changed_from=self.changed_from,
            changed_before=self.changed_before,
            actor_id=self.actor_id,
            actor_kind=self.actor_kind,
            actor_role=self.actor_role,
            operation=operation,
            field_name=field_name,
        )


class DimensionParameters(LogParameters):
    dimension_id: Annotated[
        UUID | None, Field(description="해당 Dimension UUID의 로그만 조회합니다.")
    ] = None
    operation: Annotated[
        DimensionOperation | None,
        Field(description="Dimension 작업 종류와 일치하는 로그만 조회합니다."),
    ] = None
    field_name: Annotated[
        AuditField | None, Field(description="CODE, VALUE 또는 DELETED 필드의 로그만 조회합니다.")
    ] = None


class MasterCodeParameters(LogParameters):
    master_code_id: Annotated[
        UUID | None, Field(description="해당 MasterCode UUID의 로그만 조회합니다.")
    ] = None
    operation: Annotated[
        MasterCodeOperation | None,
        Field(description="MasterCode 작업 종류와 일치하는 로그만 조회합니다."),
    ] = None


_PAGE_DESCRIPTION = (
    "ADMIN·SUPER_ADMIN만 조회할 수 있습니다. 응답은 items와 next_cursor이며 전체 건수는 "
    "제공하지 않습니다. 같은 경로와 검색 조건으로 next_cursor를 전달하며 limit은 변경할 수 "
    "있습니다. 경로·조건이 바뀌면 422이므로 cursor를 생략하고 다시 조회해 주세요. "
    "조회 시작 전에 커밋된 로그는 같은 조건으로 끝까지 조회하면 중복·누락 없이 반환합니다. "
    "조회 중 커밋된 로그는 정렬 위치에 따라 포함되거나 제외될 수 있어 전체 확인에는 "
    "첫 페이지부터 재조회가 필요합니다."
)


def _responses(path: str) -> dict[int | str, dict[str, Any]]:
    result: dict[int | str, dict[str, Any]] = {
        401: INVALID_ACCESS_TOKEN_RESPONSE,
        403: AUTHORIZATION_DENIED_RESPONSE,
    }
    for status, code, title, detail in (
        (
            422,
            "VALIDATION_ERROR",
            "입력값 검증 실패",
            "필터·기간·UUID·페이지 크기 또는 cursor가 유효하지 않습니다.",
        ),
        (
            503,
            "SERVICE_UNAVAILABLE",
            "서비스를 사용할 수 없음",
            "감사 로그를 일시적으로 조회할 수 없습니다. 잠시 후 다시 시도해 주세요.",
        ),
    ):
        result[status] = {
            "model": ProblemDetails,
            "description": detail,
            "content": {
                "application/problem+json": {
                    "schema": {"$ref": "#/components/schemas/ProblemDetails"},
                    "example": {
                        "type": f"/problems/{code.lower().replace('_', '-')}",
                        "title": title,
                        "status": status,
                        "detail": detail,
                        "code": code,
                        "instance": path,
                    },
                }
            },
        }
    return result


async def audit_log_unavailable_handler(request: Request, exc: Exception) -> JSONResponse:
    return problem_response(
        ProblemDetails(
            type="/problems/service-unavailable",
            title="서비스를 사용할 수 없음",
            status=503,
            detail="감사 로그를 일시적으로 조회할 수 없습니다. 잠시 후 다시 시도해 주세요.",
            code="SERVICE_UNAVAILABLE",
            instance=request.url.path,
        )
    )


def _json_value(value: object) -> str:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if isinstance(value, UUID):
        return str(value)
    raise TypeError("unsupported cursor value")


def _fingerprint(query: AuditLogQuery, path: str) -> str:
    payload = json.dumps(
        {"path": path, "query": asdict(query)},
        default=_json_value,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _encode_cursor(cursor: AuditLogCursor, query: AuditLogQuery, path: str) -> str:
    payload = json.dumps(
        {
            "v": 1,
            "q": _fingerprint(query, path),
            "t": _json_value(cursor.changed_at),
            "k": cursor.source_type.kind,
            "s": cursor.source_type.value,
            "i": str(cursor.id),
        },
        separators=(",", ":"),
    )
    return base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")


def _decode_cursor(encoded: str | None, query: AuditLogQuery, path: str) -> AuditLogCursor | None:
    if encoded is None:
        return None
    try:
        data = json.loads(
            base64.b64decode(encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True)
        )
        if not isinstance(data, dict) or set(data) != {"v", "q", "t", "k", "s", "i"}:
            raise ValueError
        if type(data["v"]) is not int or data["v"] != 1 or data["q"] != _fingerprint(query, path):
            raise ValueError
        source = AuditSource(data["s"])
        if data["k"] != source.kind or (query.source is not None and source != query.source):
            raise ValueError
        cursor = AuditLogCursor(_timestamp(data["t"]), source, UUID(data["i"]))
        if _encode_cursor(cursor, query, path) != encoded:
            raise ValueError
        return cursor
    except (ValueError, TypeError, KeyError, AttributeError, UnicodeError, RecursionError):
        raise DimensionValidationError(
            "query.cursor",
            "cursor가 유효하지 않거나 경로·필터와 다릅니다. 생략하고 다시 조회해 주세요.",
        ) from None


def _page(page: AuditLogPage, query: AuditLogQuery, path: str) -> dict[str, object]:
    cursor = None
    if page.has_more and page.items:
        last = page.items[-1]
        cursor = _encode_cursor(
            AuditLogCursor(last.changed_at, last.source_type, last.id), query, path
        )
    return {"items": page.items, "next_cursor": cursor}


_DIMENSIONS = (
    (AuditSource.COMPANY, "companies", CompanyLogResponse),
    (AuditSource.MODEL, "models", ModelLogResponse),
    (AuditSource.BRAND, "brands", BrandLogResponse),
    (AuditSource.COUNTRY, "countries", CountryLogResponse),
    (AuditSource.CATEGORY, "categories", CategoryLogResponse),
    (AuditSource.YEAR, "years", YearLogResponse),
    (AuditSource.NETWORK, "networks", NetworkLogResponse),
    (AuditSource.MEMORY, "memories", MemoryLogResponse),
)


def build_audit_log_router(
    *,
    use_case: ListAuditLogs,
    principal_dependency: Callable[..., HumanPrincipal],
    authorization: AuthorizationPolicy,
) -> APIRouter:
    router = APIRouter(prefix=f"{API_V1_PREFIX}/admin", tags=["AuditLogs"])
    guard = build_authorization_guard(
        principal_dependency, authorization, AuthorizationAction.READ_AUDIT_LOG
    )

    def add_dimension(source: AuditSource, collection: str, entry_type: Any) -> None:
        path = f"/dimension-logs/{collection}"

        @router.get(
            path,
            operation_id=f"admin_list_{source.value.lower()}_dimension_logs",
            response_model=AuditLogListResponse[entry_type],
            status_code=200,
            summary=f"{source.value.title()} 변경 로그 조회",
            description=_PAGE_DESCRIPTION
            + " 변경 시각 내림차순, 같은 시각에서는 로그 UUID 내림차순입니다. "
            "제공한 필터를 모두 만족하는 로그를 반환하며, 생략한 필터에는 제한이 없습니다.",
            responses=_responses(f"{API_V1_PREFIX}/admin{path}"),
        )
        async def list_dimension(
            request: Request,
            principal: Annotated[HumanPrincipal, Depends(guard)],
            params: Annotated[DimensionParameters, Query()],
        ) -> dict[str, object]:
            query = AuditLogQuery(
                source,
                params.change_set_id,
                params.filters(
                    entity_id=params.dimension_id,
                    operation=params.operation,
                    field_name=params.field_name,
                ),
            )
            after = _decode_cursor(params.cursor, query, request.url.path)
            return _page(
                await use_case.execute(principal, query, after=after, limit=params.limit),
                query,
                request.url.path,
            )

    for source, collection, entry_type in _DIMENSIONS:
        add_dimension(source, collection, entry_type)

    @router.get(
        "/master-code-logs",
        operation_id="admin_list_master_code_logs",
        response_model=AuditLogListResponse[MasterCodeLogResponse],
        status_code=200,
        summary="MasterCode 변경 로그 조회",
        description=_PAGE_DESCRIPTION
        + " 변경 시각 내림차순, 같은 시각에서는 로그 UUID 내림차순입니다. "
        "제공한 필터를 모두 만족하는 로그를 반환하며, 생략한 필터에는 제한이 없습니다. "
        "참조·code·삭제 여부는 변경 당시 기록된 값을 반환합니다.",
        responses=_responses(f"{API_V1_PREFIX}/admin/master-code-logs"),
    )
    async def list_master_codes(
        request: Request,
        principal: Annotated[HumanPrincipal, Depends(guard)],
        params: Annotated[MasterCodeParameters, Query()],
    ) -> dict[str, object]:
        query = AuditLogQuery(
            AuditSource.MASTER_CODE,
            params.change_set_id,
            params.filters(entity_id=params.master_code_id, operation=params.operation),
        )
        after = _decode_cursor(params.cursor, query, request.url.path)
        return _page(
            await use_case.execute(principal, query, after=after, limit=params.limit),
            query,
            request.url.path,
        )

    @router.get(
        "/change-sets/{change_set_id}/logs",
        operation_id="admin_get_change_set_logs",
        response_model=AuditLogListResponse[AuditLogResponse],
        status_code=200,
        summary="변경 묶음의 전체 로그 조회",
        description=_PAGE_DESCRIPTION
        + " 지정한 change set의 Dimension·MasterCode 로그를 함께 반환합니다. "
        "changed_at 내림차순, source_kind·source_type 오름차순, id 내림차순이며 "
        "출처는 표시된 대문자 식별자의 사전순입니다. source_kind가 DIMENSION이면 "
        "field_name과 타입별 전후 값을, MASTER_CODE이면 old_state·new_state를 읽습니다. "
        "cursor와 limit만 허용하며 검색 필터는 422입니다. 해당 로그가 없으면 200과 빈 items, "
        "next_cursor: null을 반환합니다. 한 변경 묶음의 로그는 함께 커밋됩니다.",
        responses=_responses(f"{API_V1_PREFIX}/admin/change-sets/{{change_set_id}}/logs"),
    )
    async def list_change_set(
        request: Request,
        change_set_id: Annotated[
            UUID, Path(description="전체 로그를 조회할 변경 묶음의 UUID입니다.")
        ],
        principal: Annotated[HumanPrincipal, Depends(guard)],
        params: Annotated[PageParameters, Query()],
    ) -> dict[str, object]:
        query = AuditLogQuery(change_set_id=change_set_id)
        after = _decode_cursor(params.cursor, query, request.url.path)
        return _page(
            await use_case.execute(principal, query, after=after, limit=params.limit),
            query,
            request.url.path,
        )

    return router
