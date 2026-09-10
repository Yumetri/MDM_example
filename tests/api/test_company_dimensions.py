from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from uuid import UUID

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from mdm.api.auth import build_human_principal_dependency
from mdm.api.dimensions import build_company_router
from mdm.application.audit import HumanMutationAuditFactory
from mdm.application.auth import (
    AccessTokenClaims,
    AuthenticateHumanPrincipal,
    HumanPrincipal,
    InvalidAccessToken,
    OperationalEvent,
)
from mdm.application.authorization import AuthorizationPolicy
from mdm.application.dimensions import (
    CompanyCodeConflict,
    CompanyCursor,
    CompanyMultipleConflicts,
    CompanyNotFound,
    CompanyPage,
    CompanyRepository,
    CompanyRepositoryUnavailable,
    CompanyValueConflict,
    CreateCompany,
    GetCompany,
    ListCompanies,
    UpdateCompanyValue,
)
from mdm.application.master_codes import MasterCodeConflict
from mdm.application.preconditions import PreconditionFailed
from mdm.domain.audit import MutationAuditMetadata
from mdm.domain.auth import UserRole
from mdm.domain.dimensions import CompanyValue, Dimension, DimensionCode
from mdm.main import create_app

USER_ID = UUID("01890f7c-8abc-7def-8abc-111111111111")
COMPANY_ID = UUID("01890f7c-8abc-7def-8abc-222222222222")
JTI = UUID("123e4567-e89b-42d3-a456-426614174000")
CHANGE_SET_ID = UUID("01890f7c-8abc-7def-8abc-333333333333")
NOW = datetime(2033, 5, 18, 3, 33, 20, tzinfo=UTC)
LATER = datetime(2033, 5, 18, 3, 33, 21, tzinfo=UTC)


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


class FakeCompanyRepository(CompanyRepository):
    def __init__(self) -> None:
        self.company = Dimension(
            id=COMPANY_ID,
            code=DimensionCode("SAM"),
            value=CompanyValue("SAMSUNG_ELECTRONICS"),
            version=1,
            created_at=NOW,
            updated_at=NOW,
            deleted_at=None,
        )
        self.created: tuple[DimensionCode, CompanyValue, MutationAuditMetadata] | None = None
        self.list_after: CompanyCursor | None = None
        self.updated: (
            tuple[UUID, int, CompanyValue | None, MutationAuditMetadata, DimensionCode | None]
            | None
        ) = None
        self.failure: Exception | None = None

    async def create(
        self,
        code: DimensionCode,
        value: CompanyValue,
        audit: MutationAuditMetadata,
    ) -> Dimension[CompanyValue]:
        if self.failure is not None:
            raise self.failure
        self.created = (code, value, audit)
        return self.company

    async def get_active(self, company_id: UUID) -> Dimension[CompanyValue]:
        if self.failure is not None:
            raise self.failure
        if company_id != COMPANY_ID:
            raise CompanyNotFound
        return self.company

    async def list_active(self, *, after: CompanyCursor | None, limit: int) -> CompanyPage:
        self.list_after = after
        return CompanyPage(items=(self.company,), has_more=after is None)

    async def update_value(self, company_id, expected_version, value, audit, *, code=None):
        if self.failure is not None:
            raise self.failure
        self.updated = (company_id, expected_version, value, audit, code)
        return self.company.change(code=code, value=value, changed_at=LATER)


def build_application(repository: FakeCompanyRepository) -> FastAPI:
    application = create_app(readiness_check=HealthyReadinessCheck())
    authentication = AuthenticateHumanPrincipal(RoleVerifier(), NullEventSink(), clock=lambda: NOW)
    principal_dependency: Callable[..., HumanPrincipal] = build_human_principal_dependency(
        authentication
    )
    policy = AuthorizationPolicy()
    application.include_router(
        build_company_router(
            create_company=CreateCompany(
                repository,
                policy,
                HumanMutationAuditFactory(change_set_ids=lambda: CHANGE_SET_ID),
            ),
            get_company=GetCompany(repository, policy),
            list_companies=ListCompanies(repository, policy),
            update_company=UpdateCompanyValue(
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
async def client_for(
    repository: FakeCompanyRepository,
) -> AsyncGenerator[AsyncClient]:
    transport = ASGITransport(app=build_application(repository), raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest.mark.api
async def test_admin_can_create_normalized_company_with_etag() -> None:
    repository = FakeCompanyRepository()

    async with client_for(repository) as client:
        response = await client.post(
            "/api/v1/dimensions/companies?actor_role=SUPER_ADMIN&actor_kind=SYSTEM",
            headers={"Authorization": "Bearer ADMIN", "X-Actor-Role": "SUPER_ADMIN"},
            json={
                "code": "sam",
                "value": " Samsung  Electronics ",
                "reason": "최초 등록",
            },
        )

    assert response.status_code == 201
    assert response.headers["etag"] == '"1"'
    assert response.json() == {
        "id": str(COMPANY_ID),
        "code": "SAM",
        "value": "SAMSUNG_ELECTRONICS",
        "version": 1,
        "created_at": "2033-05-18T03:33:20Z",
        "updated_at": "2033-05-18T03:33:20Z",
        "deleted_at": None,
    }
    assert repository.created is not None
    assert repository.created[2].actor.role is UserRole.ADMIN
    assert repository.created[2].actor.actor_id == str(USER_ID)


@pytest.mark.api
async def test_create_validates_reason_length_after_trimming_edge_spaces() -> None:
    repository = FakeCompanyRepository()

    async with client_for(repository) as client:
        response = await client.post(
            "/api/v1/dimensions/companies",
            headers={"Authorization": "Bearer ADMIN"},
            json={"code": "SAM", "value": "Samsung", "reason": f" {'A' * 500} "},
        )

    assert response.status_code == 201
    assert repository.created is not None
    assert repository.created[2].reason == "A" * 500


@pytest.mark.api
async def test_user_direct_creation_is_forbidden_but_reads_are_allowed() -> None:
    repository = FakeCompanyRepository()

    async with client_for(repository) as client:
        denied = await client.post(
            "/api/v1/dimensions/companies?actor_role=SUPER_ADMIN&actor_kind=SYSTEM",
            headers={
                "Authorization": "Bearer USER",
                "X-Actor-Role": "SUPER_ADMIN",
                "X-Actor-Kind": "SYSTEM",
            },
            json={"code": "SAM", "value": "Samsung"},
        )
        allowed = await client.get(
            f"/api/v1/dimensions/companies/{COMPANY_ID}",
            headers={"Authorization": "Bearer USER"},
        )

    assert denied.status_code == 403
    assert denied.json()["code"] == "AUTHORIZATION_DENIED"
    assert repository.created is None
    assert allowed.status_code == 200
    assert allowed.headers["etag"] == '"1"'


@pytest.mark.api
async def test_admin_updates_company_value_with_if_match_and_new_etag() -> None:
    repository = FakeCompanyRepository()

    async with client_for(repository) as client:
        response = await client.patch(
            f"/api/v1/dimensions/companies/{COMPANY_ID}",
            headers={"Authorization": "Bearer ADMIN", "If-Match": '"1"'},
            json={"value": " Apple  Korea ", "reason": "수정"},
        )

    assert response.status_code == 200
    assert response.headers["etag"] == '"2"'
    assert response.json()["value"] == "APPLE_KOREA"
    assert response.json()["version"] == 2
    assert repository.updated is not None
    assert repository.updated[1] == 1
    assert repository.updated[2] == CompanyValue("APPLE_KOREA")


@pytest.mark.api
@pytest.mark.parametrize(
    ("headers", "expected_status", "expected_code"),
    [
        ({}, 428, "PRECONDITION_REQUIRED"),
        ({"If-Match": "*"}, 400, "INVALID_IF_MATCH"),
        ({"If-Match": 'W/"1"'}, 400, "INVALID_IF_MATCH"),
        ({"If-Match": '"1", "2"'}, 400, "INVALID_IF_MATCH"),
    ],
)
async def test_company_update_enforces_strong_if_match_contract(
    headers: dict[str, str], expected_status: int, expected_code: str
) -> None:
    repository = FakeCompanyRepository()

    async with client_for(repository) as client:
        response = await client.patch(
            f"/api/v1/dimensions/companies/{COMPANY_ID}",
            headers={"Authorization": "Bearer ADMIN", **headers},
            json={"value": "Apple"},
        )

    assert response.status_code == expected_status
    assert response.json()["code"] == expected_code
    assert repository.updated is None


@pytest.mark.api
async def test_company_update_rejects_repeated_if_match_header_lines() -> None:
    repository = FakeCompanyRepository()

    async with client_for(repository) as client:
        response = await client.patch(
            f"/api/v1/dimensions/companies/{COMPANY_ID}",
            headers=[
                ("Authorization", "Bearer ADMIN"),
                ("If-Match", '"1"'),
                ("If-Match", '"2"'),
            ],
            json={"value": "Apple"},
        )

    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_IF_MATCH"
    assert repository.updated is None


@pytest.mark.api
async def test_stale_company_update_is_rejected() -> None:
    repository = FakeCompanyRepository()
    repository.failure = PreconditionFailed()

    async with client_for(repository) as client:
        stale = await client.patch(
            f"/api/v1/dimensions/companies/{COMPANY_ID}",
            headers={"Authorization": "Bearer ADMIN", "If-Match": '"1"'},
            json={"value": "Apple"},
        )

    assert stale.status_code == 412
    assert stale.json()["code"] == "PRECONDITION_FAILED"


@pytest.mark.api
async def test_master_code_recomposition_conflict_uses_stable_problem_code() -> None:
    repository = FakeCompanyRepository()
    repository.failure = MasterCodeConflict()

    async with client_for(repository) as client:
        response = await client.patch(
            f"/api/v1/dimensions/companies/{COMPANY_ID}",
            headers={"Authorization": "Bearer ADMIN", "If-Match": '"1"'},
            json={"code": "APP"},
        )

    assert response.status_code == 409
    assert response.json()["code"] == "MASTER_CODE_CONFLICT"
    assert "internal" not in response.text


@pytest.mark.api
async def test_admin_can_update_company_code_without_supplying_value() -> None:
    repository = FakeCompanyRepository()

    async with client_for(repository) as client:
        response = await client.patch(
            f"/api/v1/dimensions/companies/{COMPANY_ID}",
            headers={"Authorization": "Bearer ADMIN", "If-Match": '"1"'},
            json={"code": "app", "reason": "코드 수정"},
        )

    assert response.status_code == 200
    assert response.headers["etag"] == '"2"'
    assert response.json()["code"] == "APP"
    assert response.json()["value"] == "SAMSUNG_ELECTRONICS"
    assert repository.updated is not None
    assert repository.updated[2] is None
    assert repository.updated[4] == DimensionCode("APP")


@pytest.mark.api
@pytest.mark.parametrize("payload", [{}, {"reason": "수정"}, {"code": None}, {"value": None}])
async def test_company_update_requires_non_null_code_or_value(payload: dict[str, object]) -> None:
    repository = FakeCompanyRepository()

    async with client_for(repository) as client:
        response = await client.patch(
            f"/api/v1/dimensions/companies/{COMPANY_ID}",
            headers={"Authorization": "Bearer ADMIN", "If-Match": '"1"'},
            json=payload,
        )

    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"
    assert repository.updated is None


@pytest.mark.api
async def test_actor_fields_in_create_body_are_rejected_instead_of_trusted() -> None:
    repository = FakeCompanyRepository()

    async with client_for(repository) as client:
        response = await client.post(
            "/api/v1/dimensions/companies",
            headers={"Authorization": "Bearer ADMIN"},
            json={
                "code": "SAM",
                "value": "Samsung",
                "actor_id": str(USER_ID),
                "actor_role": "SUPER_ADMIN",
                "actor_kind": "SYSTEM",
            },
        )

    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"
    assert repository.created is None


@pytest.mark.api
async def test_list_uses_opaque_descending_cursor_and_no_total_count() -> None:
    repository = FakeCompanyRepository()

    async with client_for(repository) as client:
        first = await client.get(
            "/api/v1/dimensions/companies?limit=1",
            headers={"Authorization": "Bearer SUPER_ADMIN"},
        )
        cursor = first.json()["next_cursor"]
        second = await client.get(
            "/api/v1/dimensions/companies",
            params={"limit": 1, "cursor": cursor},
            headers={"Authorization": "Bearer USER"},
        )

    assert first.status_code == 200
    assert set(first.json()) == {"items", "next_cursor"}
    assert cursor and "2033" not in cursor and str(COMPANY_ID) not in cursor
    assert second.status_code == 200
    assert second.json()["next_cursor"] is None
    assert repository.list_after == CompanyCursor(created_at=NOW, id=COMPANY_ID)


@pytest.mark.api
@pytest.mark.parametrize("query", ["limit=0", "limit=101", "cursor=not-a-cursor"])
async def test_invalid_pagination_returns_rfc_9457_validation_error(query: str) -> None:
    async with client_for(FakeCompanyRepository()) as client:
        response = await client.get(
            f"/api/v1/dimensions/companies?{query}",
            headers={"Authorization": "Bearer USER"},
        )

    assert response.status_code == 422
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["code"] == "VALIDATION_ERROR"


@pytest.mark.api
@pytest.mark.parametrize(
    ("failure", "code"),
    [
        (CompanyCodeConflict(), "DIMENSION_CODE_CONFLICT"),
        (CompanyValueConflict(), "DIMENSION_VALUE_CONFLICT"),
        (CompanyMultipleConflicts(), "DIMENSION_MULTIPLE_CONFLICTS"),
    ],
)
async def test_create_conflicts_use_stable_problem_codes(failure: Exception, code: str) -> None:
    repository = FakeCompanyRepository()
    repository.failure = failure

    async with client_for(repository) as client:
        response = await client.post(
            "/api/v1/dimensions/companies",
            headers={"Authorization": "Bearer ADMIN"},
            json={"code": "SAM", "value": "Samsung"},
        )

    assert response.status_code == 409
    assert response.json()["code"] == code
    assert "internal" not in response.text


@pytest.mark.api
async def test_missing_company_uses_stable_not_found_problem() -> None:
    async with client_for(FakeCompanyRepository()) as client:
        response = await client.get(
            "/api/v1/dimensions/companies/01890f7c-8abc-7def-8abc-999999999999",
            headers={"Authorization": "Bearer USER"},
        )

    assert response.status_code == 404
    assert response.json()["code"] == "DIMENSION_NOT_FOUND"


@pytest.mark.api
async def test_temporary_company_repository_failure_is_sanitized() -> None:
    repository = FakeCompanyRepository()
    repository.failure = CompanyRepositoryUnavailable()

    async with client_for(repository) as client:
        response = await client.post(
            "/api/v1/dimensions/companies",
            headers={"Authorization": "Bearer ADMIN"},
            json={"code": "SAM", "value": "Samsung"},
        )

    assert response.status_code == 503
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["type"] == "/problems/service-unavailable"
    assert response.json()["code"] == "SERVICE_UNAVAILABLE"
    assert "database" not in response.text.lower()


@pytest.mark.api
def test_company_openapi_is_consumer_oriented_and_documents_security_and_errors() -> None:
    schema = build_application(FakeCompanyRepository()).openapi()
    paths = schema["paths"]

    create = paths["/api/v1/dimensions/companies"]["post"]
    listing = paths["/api/v1/dimensions/companies"]["get"]
    detail = paths["/api/v1/dimensions/companies/{company_id}"]["get"]
    update = paths["/api/v1/dimensions/companies/{company_id}"]["patch"]
    assert create["operationId"] == "create_company_dimension"
    assert listing["operationId"] == "list_company_dimensions"
    assert detail["operationId"] == "get_company_dimension"
    assert update["operationId"] == "update_company_dimension_value"
    if_match = next(
        parameter for parameter in update["parameters"] if parameter["name"] == "If-Match"
    )
    assert if_match["required"] is True
    assert if_match["schema"]["type"] == "string"
    assert "anyOf" not in if_match["schema"]
    assert if_match["schema"]["pattern"] == '^"[1-9][0-9]*"$'
    assert if_match["schema"]["x-version-maximum"] == 2_147_483_647
    assert create["security"] == [{"BearerAuth": []}]
    assert {"401", "403", "409", "422", "503"} <= set(create["responses"])
    assert {"401", "404", "422", "503"} <= set(detail["responses"])
    assert {"400", "401", "403", "404", "409", "412", "422", "428", "503"} <= set(
        update["responses"]
    )
    assert update["responses"]["200"]["headers"]["ETag"]["schema"]["example"] == '"1"'
    assert "503" in listing["responses"]
    conflict_examples = create["responses"]["409"]["content"]["application/problem+json"][
        "examples"
    ]
    assert conflict_examples["codeConflict"]["value"]["violations"] == [
        {"field": "body.code", "message": "이미 사용 중인 값입니다."}
    ]
    assert conflict_examples["valueConflict"]["value"]["violations"] == [
        {"field": "body.value", "message": "이미 사용 중인 값입니다."}
    ]
    assert conflict_examples["multipleConflicts"]["value"]["violations"] == [
        {"field": "body.code", "message": "이미 사용 중인 값입니다."},
        {"field": "body.value", "message": "이미 사용 중인 값입니다."},
    ]
    assert create["responses"]["201"]["headers"]["ETag"]["schema"]["example"] == '"1"'
    assert detail["responses"]["200"]["headers"]["ETag"]["schema"]["example"] == '"1"'
    assert listing["parameters"][0]["name"] in {"cursor", "limit"}
    schemas = schema["components"]["schemas"]
    assert schemas["CompanyResponse"]["properties"]["code"]["description"]
    assert schemas["CompanyCreateRequest"]["properties"]["code"]["minLength"] == 1
    assert schemas["CompanyCreateRequest"]["properties"]["code"]["maxLength"] == 32
    assert schemas["CompanyCreateRequest"]["properties"]["value"]["x-normalized-maxLength"] == 128
    assert (
        schemas["CompanyListResponse"]["properties"]["next_cursor"]["anyOf"][0]["maxLength"] == 512
    )
