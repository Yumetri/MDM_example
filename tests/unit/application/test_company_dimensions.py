from datetime import UTC, datetime
from uuid import UUID

import pytest

from mdm.application.audit import HumanMutationAuditFactory
from mdm.application.auth import HumanPrincipal
from mdm.application.authorization import AuthorizationDenied, AuthorizationPolicy
from mdm.application.dimensions import (
    CompanyCursor,
    CompanyPage,
    CompanyRepository,
    CreateCompany,
    GetCompany,
    ListCompanies,
)
from mdm.domain.audit import DimensionOperation, MutationAuditMetadata
from mdm.domain.auth import UserRole
from mdm.domain.dimensions import CompanyValue, Dimension, DimensionCode

ADMIN_ID = UUID("01890f7c-8abc-7def-8abc-111111111111")
USER_ID = UUID("01890f7c-8abc-7def-8abc-222222222222")
COMPANY_ID = UUID("01890f7c-8abc-7def-8abc-333333333333")
CHANGE_SET_ID = UUID("01890f7c-8abc-7def-8abc-444444444444")
NOW = datetime(2033, 5, 18, tzinfo=UTC)


class RecordingCompanyRepository(CompanyRepository):
    def __init__(self) -> None:
        self.create_call: tuple[DimensionCode, CompanyValue, MutationAuditMetadata] | None = None
        self.get_call: UUID | None = None
        self.list_call: tuple[CompanyCursor | None, int] | None = None
        self.company = Dimension(
            id=COMPANY_ID,
            code=DimensionCode("SAM"),
            value=CompanyValue("SAMSUNG"),
            version=1,
            created_at=NOW,
            updated_at=NOW,
            deleted_at=None,
        )

    async def create(
        self,
        code: DimensionCode,
        value: CompanyValue,
        audit: MutationAuditMetadata,
    ) -> Dimension[CompanyValue]:
        self.create_call = (code, value, audit)
        return self.company

    async def get_active(self, company_id: UUID) -> Dimension[CompanyValue]:
        self.get_call = company_id
        return self.company

    async def list_active(
        self,
        *,
        after: CompanyCursor | None,
        limit: int,
    ) -> CompanyPage:
        self.list_call = (after, limit)
        return CompanyPage(items=(self.company,), has_more=False)


def _principal(role: UserRole) -> HumanPrincipal:
    return HumanPrincipal(user_id=ADMIN_ID if role is not UserRole.USER else USER_ID, role=role)


@pytest.mark.unit
async def test_admin_creation_normalizes_input_and_builds_one_human_create_context() -> None:
    repository = RecordingCompanyRepository()
    use_case = CreateCompany(
        repository,
        AuthorizationPolicy(),
        HumanMutationAuditFactory(change_set_ids=lambda: CHANGE_SET_ID),
    )

    result = await use_case.execute(
        _principal(UserRole.ADMIN),
        code="sam",
        value="  Samsung  Electronics ",
        reason="  최초 등록  ",
    )

    assert result is repository.company
    assert repository.create_call is not None
    code, value, audit = repository.create_call
    assert code.value == "SAM"
    assert value.value == "SAMSUNG_ELECTRONICS"
    assert audit.change_set_id == CHANGE_SET_ID
    assert audit.actor.actor_id == str(ADMIN_ID)
    assert audit.actor.role is UserRole.ADMIN
    assert audit.operations.dimension is DimensionOperation.CREATE
    assert audit.reason == "최초 등록"


@pytest.mark.unit
async def test_user_cannot_directly_create_a_company() -> None:
    repository = RecordingCompanyRepository()
    use_case = CreateCompany(
        repository,
        AuthorizationPolicy(),
        HumanMutationAuditFactory(change_set_ids=lambda: CHANGE_SET_ID),
    )

    with pytest.raises(AuthorizationDenied):
        await use_case.execute(_principal(UserRole.USER), code="SAM", value="SAMSUNG", reason=None)

    assert repository.create_call is None


@pytest.mark.unit
async def test_every_human_role_can_get_and_list_active_companies() -> None:
    for role in UserRole:
        repository = RecordingCompanyRepository()
        get_company = GetCompany(repository, AuthorizationPolicy())
        list_companies = ListCompanies(repository, AuthorizationPolicy())
        cursor = CompanyCursor(created_at=NOW, id=COMPANY_ID)

        assert await get_company.execute(_principal(role), COMPANY_ID) is repository.company
        page = await list_companies.execute(_principal(role), after=cursor, limit=50)

        assert page.items == (repository.company,)
        assert repository.get_call == COMPANY_ID
        assert repository.list_call == (cursor, 50)


@pytest.mark.unit
@pytest.mark.parametrize("limit", [0, 101, True])
async def test_list_limit_must_be_an_integer_between_one_and_one_hundred(
    limit: int,
) -> None:
    repository = RecordingCompanyRepository()
    use_case = ListCompanies(repository, AuthorizationPolicy())

    with pytest.raises(ValueError):
        await use_case.execute(_principal(UserRole.USER), after=None, limit=limit)

    assert repository.list_call is None
