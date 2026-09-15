import base64
import hashlib
import json
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from uuid import UUID

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from mdm.api.auth import build_human_principal_dependency
from mdm.api.master_codes import CURSOR_EXAMPLE, ETAG_EXAMPLE, build_master_code_router
from mdm.application.audit import HumanMutationAuditFactory
from mdm.application.auth import (
    AccessTokenClaims,
    AuthenticateHumanPrincipal,
    HumanPrincipal,
    InvalidAccessToken,
    OperationalEvent,
)
from mdm.application.authorization import AuthorizationPolicy
from mdm.application.master_codes import (
    CreateMasterCode,
    ExistingDimension,
    GetMasterCode,
    InlineDimension,
    InlineDimensionConflict,
    InvalidDimensionReference,
    ListMasterCodes,
    MasterCodeConflict,
    MasterCodeCreatePlan,
    MasterCodeCursor,
    MasterCodeNotFound,
    MasterCodePage,
    MasterCodeReferenceUpdate,
    MasterCodeRepository,
    NotApplicable,
    UpdateMasterCodeReferences,
)
from mdm.application.preconditions import PreconditionFailed
from mdm.domain.audit import MasterCodeOperation, MutationAuditMetadata
from mdm.domain.auth import UserRole
from mdm.domain.dimensions import CompanyValue, DimensionCode
from mdm.domain.master_codes import MasterCode, MasterCodeDimensions
from mdm.main import create_app

USER_ID = UUID("01890f7c-8abc-7def-8abc-111111111111")
MASTER_CODE_ID = UUID("01890f7c-8abc-7def-8abc-222222222222")
JTI = UUID("123e4567-e89b-42d3-a456-426614174000")
CHANGE_SET_ID = UUID("01890f7c-8abc-7def-8abc-333333333333")
NOW = datetime(2033, 5, 18, 3, 33, 20, tzinfo=UTC)


class HealthyReadinessCheck:
    async def execute(self) -> None:
        return None


class RoleVerifier:
    def verify(self, token: str, *, now: int) -> AccessTokenClaims:
        try:
            role = UserRole(token)
        except ValueError:
            raise InvalidAccessToken from None
        return AccessTokenClaims(
            user_id=USER_ID,
            role=role,
            issued_at=now - 1,
            expires_at=now + 899,
            jti=JTI,
        )


class NullEventSink:
    def emit(self, event: OperationalEvent) -> None:
        del event


class FakeMasterCodeRepository(MasterCodeRepository):
    def __init__(self) -> None:
        self.master_code = MasterCode.create(
            id=MASTER_CODE_ID,
            dimensions=MasterCodeDimensions(),
            created_at=NOW,
        )
        self.created: tuple[MasterCodeCreatePlan, MutationAuditMetadata] | None = None
        self.list_after: MasterCodeCursor | None = None
        self.create_failure: Exception | None = None
        self.update_failure: Exception | None = None
        self.updated: tuple[UUID, MasterCodeReferenceUpdate, str, MutationAuditMetadata] | None = (
            None
        )

    async def create(self, plan: MasterCodeCreatePlan, audit: MutationAuditMetadata) -> MasterCode:
        if self.create_failure is not None:
            raise self.create_failure
        self.created = (plan, audit)
        return self.master_code

    async def get_active(self, master_code_id: UUID) -> MasterCode:
        return self.master_code

    async def list_active(self, *, after: MasterCodeCursor | None, limit: int) -> MasterCodePage:
        self.list_after = after
        return MasterCodePage(items=(self.master_code,), has_more=after is None)

    async def update_references(
        self,
        master_code_id: UUID,
        changes: MasterCodeReferenceUpdate,
        expected_etag: str,
        audit: MutationAuditMetadata,
    ) -> MasterCode:
        if self.update_failure is not None:
            raise self.update_failure
        self.updated = (master_code_id, changes, expected_etag, audit)
        return self.master_code


def build_application(repository: FakeMasterCodeRepository) -> FastAPI:
    application = create_app(readiness_check=HealthyReadinessCheck())
    authentication = AuthenticateHumanPrincipal(RoleVerifier(), NullEventSink(), clock=lambda: NOW)
    principal_dependency: Callable[..., HumanPrincipal] = build_human_principal_dependency(
        authentication
    )
    policy = AuthorizationPolicy()
    application.include_router(
        build_master_code_router(
            create_master_code=CreateMasterCode(
                repository,
                policy,
                HumanMutationAuditFactory(change_set_ids=lambda: CHANGE_SET_ID),
            ),
            get_master_code=GetMasterCode(repository, policy),
            list_master_codes=ListMasterCodes(repository, policy),
            update_master_code_references=UpdateMasterCodeReferences(
                repository,
                policy,
                HumanMutationAuditFactory(change_set_ids=lambda: CHANGE_SET_ID),
            ),
            principal_dependency=principal_dependency,
            authorization=policy,
        )
    )
    application.openapi_schema = None
    return application


@asynccontextmanager
async def client_for(repository: FakeMasterCodeRepository) -> AsyncGenerator[AsyncClient]:
    transport = ASGITransport(app=build_application(repository), raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


def _all_not_applicable() -> dict[str, object]:
    return {
        slot: {"mode": "NOT_APPLICABLE"}
        for slot in (
            "company",
            "brand",
            "model",
            "category",
            "year",
            "memory",
            "network",
            "country",
        )
    }


@pytest.mark.api
async def test_admin_creates_all_not_applicable_master_code() -> None:
    repository = FakeMasterCodeRepository()

    async with client_for(repository) as client:
        response = await client.post(
            "/api/v1/master-codes",
            headers={"Authorization": "Bearer ADMIN"},
            json={"dimensions": _all_not_applicable(), "reason": "등록"},
        )

    assert response.status_code == 201
    assert response.headers["etag"] == repository.master_code.etag()
    assert response.json()["code"] == "NNN-NNN-NNN-NNN-NNN-NNN-NNN-NNN"
    assert response.json()["dimensions"] == dict.fromkeys(_all_not_applicable())
    assert repository.created is not None


@pytest.mark.api
async def test_inline_company_is_normalized_and_user_cannot_create() -> None:
    repository = FakeMasterCodeRepository()
    payload = _all_not_applicable()
    payload["company"] = {"mode": "CREATE", "code": "com", "value": " Acme Corp "}

    async with client_for(repository) as client:
        denied = await client.post(
            "/api/v1/master-codes",
            headers={"Authorization": "Bearer USER"},
            json={"dimensions": payload},
        )
        created = await client.post(
            "/api/v1/master-codes",
            headers={"Authorization": "Bearer SUPER_ADMIN"},
            json={"dimensions": payload},
        )

    assert denied.status_code == 403
    assert created.status_code == 201
    assert repository.created is not None
    plan, _ = repository.created
    assert plan.company == InlineDimension(
        code=DimensionCode("COM"), value=CompanyValue("ACME_CORP")
    )


@pytest.mark.api
async def test_tagged_union_rejects_missing_slots_and_mode_extras() -> None:
    repository = FakeMasterCodeRepository()
    missing = _all_not_applicable()
    del missing["country"]
    extra = _all_not_applicable()
    extra["country"] = {"mode": "NOT_APPLICABLE", "id": str(USER_ID)}

    async with client_for(repository) as client:
        missing_response = await client.post(
            "/api/v1/master-codes",
            headers={"Authorization": "Bearer ADMIN"},
            json={"dimensions": missing},
        )
        extra_response = await client.post(
            "/api/v1/master-codes",
            headers={"Authorization": "Bearer ADMIN"},
            json={"dimensions": extra},
        )

    assert missing_response.status_code == 422
    assert extra_response.status_code == 422
    assert repository.created is None


@pytest.mark.api
async def test_inline_dimension_rejects_reserved_n_only_code() -> None:
    repository = FakeMasterCodeRepository()
    dimensions = _all_not_applicable()
    dimensions["company"] = {"mode": "CREATE", "code": "NNN", "value": "company"}

    async with client_for(repository) as client:
        response = await client.post(
            "/api/v1/master-codes",
            headers={"Authorization": "Bearer ADMIN"},
            json={"dimensions": dimensions},
        )

    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"
    assert response.json()["violations"] == [
        {
            "field": "body.dimensions.company.code",
            "message": "N으로만 이루어진 예약 코드는 사용할 수 없습니다.",
        }
    ]
    assert repository.created is None


@pytest.mark.api
async def test_authenticated_user_reads_detail_and_dimension_style_cursor_page() -> None:
    repository = FakeMasterCodeRepository()

    async with client_for(repository) as client:
        detail = await client.get(
            f"/api/v1/master-codes/{MASTER_CODE_ID}", headers={"Authorization": "Bearer USER"}
        )
        first = await client.get(
            "/api/v1/master-codes?limit=1",
            headers={"Authorization": "Bearer USER"},
        )
        second = await client.get(
            "/api/v1/master-codes",
            params={"limit": 1, "cursor": first.json()["next_cursor"]},
            headers={"Authorization": "Bearer USER"},
        )

    assert detail.status_code == 200
    assert detail.headers["etag"] == repository.master_code.etag()
    assert first.status_code == second.status_code == 200
    assert second.json()["next_cursor"] is None
    assert repository.list_after == MasterCodeCursor(created_at=NOW, id=MASTER_CODE_ID)


@pytest.mark.api
async def test_admin_partially_updates_master_code_references_with_aggregate_etag() -> None:
    repository = FakeMasterCodeRepository()
    expected_etag = '"mc-1-' + "a" * 64 + '"'

    async with client_for(repository) as client:
        response = await client.patch(
            f"/api/v1/master-codes/{MASTER_CODE_ID}",
            headers={"Authorization": "Bearer ADMIN", "If-Match": expected_etag},
            json={
                "dimensions": {
                    "company": {"mode": "REFERENCE", "id": str(USER_ID)},
                    "country": {"mode": "NOT_APPLICABLE"},
                },
                "reason": "참조 정정",
            },
        )

    assert response.status_code == 200
    assert response.headers["etag"] == repository.master_code.etag()
    assert repository.updated is not None
    master_code_id, changes, received_etag, audit = repository.updated
    assert master_code_id == MASTER_CODE_ID
    assert changes.company == ExistingDimension(USER_ID)
    assert isinstance(changes.country, NotApplicable)
    assert changes.brand is None
    assert received_etag == expected_etag
    assert audit.operations.master_code is MasterCodeOperation.REFERENCE_UPDATE


@pytest.mark.api
@pytest.mark.parametrize(
    ("headers", "status", "code"),
    [
        ({}, 428, "PRECONDITION_REQUIRED"),
        ({"If-Match": "*"}, 400, "INVALID_IF_MATCH"),
        ({"If-Match": 'W/"mc-1-' + "a" * 64 + '"'}, 400, "INVALID_IF_MATCH"),
        ({"If-Match": '"mc-1-' + "A" * 64 + '"'}, 400, "INVALID_IF_MATCH"),
    ],
)
async def test_master_code_reference_update_requires_one_canonical_strong_etag(
    headers: dict[str, str], status: int, code: str
) -> None:
    repository = FakeMasterCodeRepository()

    async with client_for(repository) as client:
        response = await client.patch(
            f"/api/v1/master-codes/{MASTER_CODE_ID}",
            headers={"Authorization": "Bearer ADMIN", **headers},
            json={"dimensions": {"company": {"mode": "NOT_APPLICABLE"}}},
        )

    assert response.status_code == status
    assert response.json()["code"] == code
    assert repository.updated is None


@pytest.mark.api
async def test_master_code_reference_update_rejects_repeated_etag_empty_patch_and_user() -> None:
    repository = FakeMasterCodeRepository()
    expected_etag = '"mc-1-' + "a" * 64 + '"'

    async with client_for(repository) as client:
        repeated = await client.patch(
            f"/api/v1/master-codes/{MASTER_CODE_ID}",
            headers=[
                ("Authorization", "Bearer ADMIN"),
                ("If-Match", expected_etag),
                ("If-Match", expected_etag),
            ],
            json={"dimensions": {"company": {"mode": "NOT_APPLICABLE"}}},
        )
        empty = await client.patch(
            f"/api/v1/master-codes/{MASTER_CODE_ID}",
            headers={"Authorization": "Bearer ADMIN", "If-Match": expected_etag},
            json={"dimensions": {}},
        )
        inline_create = await client.patch(
            f"/api/v1/master-codes/{MASTER_CODE_ID}",
            headers={"Authorization": "Bearer ADMIN", "If-Match": expected_etag},
            json={"dimensions": {"company": {"mode": "CREATE", "code": "COM", "value": "Company"}}},
        )
        denied = await client.patch(
            f"/api/v1/master-codes/{MASTER_CODE_ID}",
            headers={"Authorization": "Bearer USER", "If-Match": expected_etag},
            json={"dimensions": {"company": {"mode": "NOT_APPLICABLE"}}},
        )

    assert repeated.status_code == 400
    assert repeated.json()["code"] == "INVALID_IF_MATCH"
    assert empty.status_code == 422
    assert empty.json()["code"] == "VALIDATION_ERROR"
    assert inline_create.status_code == 422
    assert inline_create.json()["code"] == "VALIDATION_ERROR"
    assert denied.status_code == 403
    assert repository.updated is None


@pytest.mark.api
@pytest.mark.parametrize(
    ("failure", "status", "code", "violation_fields"),
    [
        (MasterCodeNotFound(), 404, "MASTER_CODE_NOT_FOUND", []),
        (MasterCodeConflict(), 409, "MASTER_CODE_CONFLICT", []),
        (PreconditionFailed(), 412, "PRECONDITION_FAILED", []),
        (
            InvalidDimensionReference(("dimensions.company.id",)),
            422,
            "INVALID_DIMENSION_REFERENCE",
            ["body.dimensions.company.id"],
        ),
    ],
)
async def test_reference_update_translates_contract_errors(
    failure: Exception,
    status: int,
    code: str,
    violation_fields: list[str],
) -> None:
    repository = FakeMasterCodeRepository()
    repository.update_failure = failure

    async with client_for(repository) as client:
        response = await client.patch(
            f"/api/v1/master-codes/{MASTER_CODE_ID}",
            headers={
                "Authorization": "Bearer ADMIN",
                "If-Match": '"mc-1-' + "a" * 64 + '"',
            },
            json={"dimensions": {"company": {"mode": "NOT_APPLICABLE"}}},
        )

    assert response.status_code == status
    assert response.json()["code"] == code
    assert [item["field"] for item in response.json().get("violations", [])] == violation_fields


@pytest.mark.api
def test_master_code_openapi_has_stable_operations_and_strict_slots() -> None:
    schema = create_app().openapi()
    collection = schema["paths"]["/api/v1/master-codes"]
    detail = schema["paths"]["/api/v1/master-codes/{master_code_id}"]

    assert collection["post"]["operationId"] == "create_master_code"
    assert collection["get"]["operationId"] == "list_master_codes"
    assert detail["get"]["operationId"] == "get_master_code"
    assert detail["patch"]["operationId"] == "update_master_code_references"
    reference_updates = schema["components"]["schemas"]["MasterCodeReferenceUpdatesInput"]
    assert reference_updates["minProperties"] == 1
    assert "required" not in reference_updates
    assert reference_updates["additionalProperties"] is False
    patch_example = detail["patch"]["responses"]["200"]["content"]["application/json"]["example"]
    assert patch_example["deleted_at"] is None
    assert patch_example["dimensions"]["model"] is None
    assert patch_example["dimensions"]["country"] is None
    patch_conflict = detail["patch"]["responses"]["409"]["content"]["application/problem+json"]
    assert "examples" not in patch_conflict
    assert patch_conflict["example"]["code"] == "MASTER_CODE_CONFLICT"
    assert patch_conflict["example"]["instance"].endswith(str(MASTER_CODE_ID))
    assert detail["patch"]["responses"]["503"]["content"]["application/problem+json"]["example"][
        "instance"
    ].endswith(str(MASTER_CODE_ID))
    dimensions = schema["components"]["schemas"]["MasterCodeDimensionsInput"]
    assert set(dimensions["required"]) == set(_all_not_applicable())
    assert dimensions["additionalProperties"] is False
    response_example = schema["components"]["schemas"]["MasterCodeResponse"]["example"]
    assert response_example["deleted_at"] is None
    assert response_example["dimensions"]["model"] is None
    assert response_example["dimensions"]["country"] is None
    cursor = next(
        parameter for parameter in collection["get"]["parameters"] if parameter["name"] == "cursor"
    )
    assert cursor["schema"]["examples"] == [CURSOR_EXAMPLE]
    assert collection["get"]["responses"]["200"]["content"]["application/json"]["example"] == {
        "items": [response_example],
        "next_cursor": CURSOR_EXAMPLE,
    }
    assert collection["post"]["responses"]["201"]["headers"]["ETag"]["schema"]["example"] == (
        ETAG_EXAMPLE
    )
    assert detail["get"]["responses"]["200"]["headers"]["ETag"]["schema"]["example"] == (
        ETAG_EXAMPLE
    )
    cursor_payload = json.loads(
        base64.urlsafe_b64decode(CURSOR_EXAMPLE + "=" * (-len(CURSOR_EXAMPLE) % 4))
    )
    assert cursor_payload == {
        "v": 1,
        "t": response_example["created_at"],
        "i": response_example["id"],
    }
    example_timestamp = datetime.fromtimestamp(
        (UUID(response_example["id"]).int >> 80) / 1000,
        tz=UTC,
    ).replace(microsecond=0)
    assert example_timestamp == datetime.fromisoformat(
        response_example["created_at"].replace("Z", "+00:00")
    )
    dimension_vector = []
    for slot in _all_not_applicable():
        dimension = response_example["dimensions"][slot]
        dimension_vector.append(
            [
                slot.upper(),
                None if dimension is None else dimension["id"],
                None if dimension is None else 1,
            ]
        )
    etag_payload = json.dumps(
        [1, response_example["version"], dimension_vector],
        separators=(",", ":"),
    ).encode()
    assert ETAG_EXAMPLE == f'"mc-1-{hashlib.sha256(etag_payload).hexdigest()}"'


@pytest.mark.api
@pytest.mark.parametrize(
    ("failure", "status", "code", "violation_fields"),
    [
        (
            InlineDimensionConflict("company", code=True, value=False),
            409,
            "DIMENSION_CODE_CONFLICT",
            ["body.dimensions.company.code"],
        ),
        (
            InlineDimensionConflict("company", code=False, value=True),
            409,
            "DIMENSION_VALUE_CONFLICT",
            ["body.dimensions.company.value"],
        ),
        (
            InlineDimensionConflict("company", code=True, value=True),
            409,
            "DIMENSION_MULTIPLE_CONFLICTS",
            ["body.dimensions.company.code", "body.dimensions.company.value"],
        ),
        (MasterCodeConflict(), 409, "MASTER_CODE_CONFLICT", []),
        (
            InvalidDimensionReference(("dimensions.company.id",)),
            422,
            "INVALID_DIMENSION_REFERENCE",
            ["body.dimensions.company.id"],
        ),
    ],
)
async def test_create_translates_master_code_contract_errors(
    failure: Exception,
    status: int,
    code: str,
    violation_fields: list[str],
) -> None:
    repository = FakeMasterCodeRepository()
    repository.create_failure = failure

    async with client_for(repository) as client:
        response = await client.post(
            "/api/v1/master-codes",
            headers={"Authorization": "Bearer ADMIN"},
            json={"dimensions": _all_not_applicable()},
        )

    assert response.status_code == status
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["code"] == code
    assert [item["field"] for item in response.json().get("violations", [])] == violation_fields
