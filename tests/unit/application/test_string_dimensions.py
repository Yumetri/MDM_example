from datetime import UTC, datetime
from uuid import UUID

import pytest

from mdm.application.audit import HumanMutationAuditFactory
from mdm.application.auth import HumanPrincipal
from mdm.application.authorization import AuthorizationDenied, AuthorizationPolicy
from mdm.application.string_dimensions import (
    CreateBrand,
    CreateCategory,
    CreateCountry,
    CreateModel,
    GetBrand,
    GetCategory,
    GetCountry,
    GetModel,
    ListBrands,
    ListCategories,
    ListCountries,
    ListModels,
    StringDimensionCursor,
    StringDimensionPage,
    UpdateBrandValue,
    UpdateCategoryValue,
    UpdateCountryValue,
    UpdateModelValue,
)
from mdm.domain.audit import DimensionOperation, MutationAuditMetadata
from mdm.domain.auth import UserRole
from mdm.domain.dimensions import (
    BrandValue,
    CategoryValue,
    CountryValue,
    Dimension,
    DimensionCode,
    ModelValue,
)

ADMIN_ID = UUID("01890f7c-8abc-7def-8abc-111111111111")
DIMENSION_ID = UUID("01890f7c-8abc-7def-8abc-222222222222")
CHANGE_SET_ID = UUID("01890f7c-8abc-7def-8abc-333333333333")
NOW = datetime(2033, 5, 18, tzinfo=UTC)


class RecordingRepository:
    def __init__(self, value: object) -> None:
        self.dimension = Dimension(
            id=DIMENSION_ID,
            code=DimensionCode("VALUE1"),
            value=value,
            version=1,
            created_at=NOW,
            updated_at=NOW,
            deleted_at=None,
        )
        self.create_call: tuple[DimensionCode, object, MutationAuditMetadata] | None = None
        self.get_call: UUID | None = None
        self.list_call: tuple[StringDimensionCursor | None, int] | None = None
        self.update_call: tuple[UUID, int, object, MutationAuditMetadata, object] | None = None

    async def create(self, code, value, audit):
        self.create_call = (code, value, audit)
        return self.dimension

    async def get_active(self, dimension_id):
        self.get_call = dimension_id
        return self.dimension

    async def list_active(self, *, after, limit):
        self.list_call = (after, limit)
        return StringDimensionPage(items=(self.dimension,), has_more=False)

    async def update_value(self, dimension_id, expected_version, value, audit, *, code=None):
        self.update_call = (dimension_id, expected_version, value, audit, code)
        return self.dimension


CASES = (
    (CreateModel, GetModel, ListModels, UpdateModelValue, ModelValue),
    (CreateBrand, GetBrand, ListBrands, UpdateBrandValue, BrandValue),
    (CreateCountry, GetCountry, ListCountries, UpdateCountryValue, CountryValue),
    (CreateCategory, GetCategory, ListCategories, UpdateCategoryValue, CategoryValue),
)


def _principal(role: UserRole) -> HumanPrincipal:
    return HumanPrincipal(user_id=ADMIN_ID, role=role)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("create_type", "get_type", "list_type", "_update_type", "value_type"), CASES
)
async def test_string_dimension_use_cases_keep_type_boundaries_and_common_policy(
    create_type: type,
    get_type: type,
    list_type: type,
    _update_type: type,
    value_type: type,
) -> None:
    repository = RecordingRepository(value_type("Galaxy S24"))
    policy = AuthorizationPolicy()
    create = create_type(
        repository,
        policy,
        HumanMutationAuditFactory(change_set_ids=lambda: CHANGE_SET_ID),
    )
    get = get_type(repository, policy)
    list_dimensions = list_type(repository, policy)

    created = await create.execute(
        _principal(UserRole.ADMIN), code="value1", value=" galaxy__s24 ", reason=" 등록 "
    )
    fetched = await get.execute(_principal(UserRole.USER), DIMENSION_ID)
    cursor = StringDimensionCursor(created_at=NOW, id=DIMENSION_ID)
    page = await list_dimensions.execute(_principal(UserRole.SUPER_ADMIN), after=cursor, limit=50)

    assert created is repository.dimension
    assert fetched is repository.dimension
    assert page.items == (repository.dimension,)
    assert repository.create_call is not None
    code, value, audit = repository.create_call
    assert code.value == "VALUE1"
    assert isinstance(value, value_type)
    assert value.value == "GALAXY_S24"
    assert audit.change_set_id == CHANGE_SET_ID
    assert audit.operations.dimension is DimensionOperation.CREATE
    assert audit.reason == "등록"
    assert repository.get_call == DIMENSION_ID
    assert repository.list_call == (cursor, 50)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("create_type", "_get_type", "_list_type", "_update_type", "value_type"), CASES
)
async def test_user_cannot_create_any_string_dimension(
    create_type: type,
    _get_type: type,
    _list_type: type,
    _update_type: type,
    value_type: type,
) -> None:
    repository = RecordingRepository(value_type("Galaxy S24"))
    create = create_type(
        repository,
        AuthorizationPolicy(),
        HumanMutationAuditFactory(change_set_ids=lambda: CHANGE_SET_ID),
    )

    with pytest.raises(AuthorizationDenied):
        await create.execute(
            _principal(UserRole.USER), code="VALUE1", value="Galaxy S24", reason=None
        )

    assert repository.create_call is None


@pytest.mark.unit
@pytest.mark.parametrize(
    ("_create_type", "_get_type", "_list_type", "update_type", "value_type"), CASES
)
async def test_admin_updates_each_string_dimension_with_normalized_value(
    _create_type: type,
    _get_type: type,
    _list_type: type,
    update_type: type,
    value_type: type,
) -> None:
    repository = RecordingRepository(value_type("Galaxy S24"))
    update = update_type(
        repository,
        AuthorizationPolicy(),
        HumanMutationAuditFactory(change_set_ids=lambda: CHANGE_SET_ID),
    )

    await update.execute(
        _principal(UserRole.ADMIN),
        DIMENSION_ID,
        expected_version=4,
        value=" Apple  Pro ",
        reason=" 수정 ",
    )

    assert repository.update_call is not None
    dimension_id, version, value, audit, code = repository.update_call
    assert dimension_id == DIMENSION_ID
    assert version == 4
    assert value == value_type("APPLE_PRO")
    assert code is None
    assert audit.operations.dimension is DimensionOperation.UPDATE
    assert audit.reason == "수정"
