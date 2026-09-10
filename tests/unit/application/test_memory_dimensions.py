from datetime import UTC, datetime
from uuid import UUID

import pytest

from mdm.application.audit import HumanMutationAuditFactory
from mdm.application.auth import HumanPrincipal
from mdm.application.authorization import AuthorizationDenied, AuthorizationPolicy
from mdm.application.memory_dimensions import (
    CreateMemory,
    GetMemory,
    ListMemories,
    MemoryDimensionCursor,
    MemoryDimensionPage,
    UpdateMemoryValue,
)
from mdm.domain.audit import DimensionOperation, MutationAuditMetadata
from mdm.domain.auth import UserRole
from mdm.domain.dimensions import Dimension, DimensionCode, MemoryUnit, MemoryValue

ADMIN_ID = UUID("01890f7c-8abc-7def-8abc-111111111111")
DIMENSION_ID = UUID("01890f7c-8abc-7def-8abc-222222222222")
CHANGE_SET_ID = UUID("01890f7c-8abc-7def-8abc-333333333333")
NOW = datetime(2033, 5, 18, tzinfo=UTC)


class RecordingMemoryRepository:
    def __init__(self) -> None:
        self.dimension = Dimension(
            id=DIMENSION_ID,
            code=DimensionCode("MEM128"),
            value=MemoryValue(amount=128, unit=MemoryUnit.GB),
            version=1,
            created_at=NOW,
            updated_at=NOW,
            deleted_at=None,
        )
        self.create_call: tuple[DimensionCode, MemoryValue, MutationAuditMetadata] | None = None
        self.get_call: UUID | None = None
        self.list_call: tuple[MemoryDimensionCursor | None, int] | None = None
        self.update_call: (
            tuple[UUID, int, MemoryValue | None, MutationAuditMetadata, DimensionCode | None] | None
        ) = None

    async def create(self, code, value, audit):
        self.create_call = (code, value, audit)
        return self.dimension

    async def get_active(self, dimension_id):
        self.get_call = dimension_id
        return self.dimension

    async def list_active(self, *, after, limit):
        self.list_call = (after, limit)
        return MemoryDimensionPage(items=(self.dimension,), has_more=False)

    async def update_value(self, dimension_id, expected_version, value, audit, *, code=None):
        self.update_call = (dimension_id, expected_version, value, audit, code)
        return self.dimension


def _principal(role: UserRole) -> HumanPrincipal:
    return HumanPrincipal(user_id=ADMIN_ID, role=role)


@pytest.mark.unit
async def test_memory_use_cases_keep_value_boundary_and_common_policy() -> None:
    repository = RecordingMemoryRepository()
    policy = AuthorizationPolicy()
    create = CreateMemory(
        repository,
        policy,
        HumanMutationAuditFactory(change_set_ids=lambda: CHANGE_SET_ID),
    )
    get = GetMemory(repository, policy)
    list_memories = ListMemories(repository, policy)

    created = await create.execute(
        _principal(UserRole.ADMIN),
        code="mem128",
        amount=128,
        unit=" gb ",
        reason=" 등록 ",
    )
    fetched = await get.execute(_principal(UserRole.USER), DIMENSION_ID)
    cursor = MemoryDimensionCursor(created_at=NOW, id=DIMENSION_ID)
    page = await list_memories.execute(_principal(UserRole.SUPER_ADMIN), after=cursor, limit=50)

    assert created is repository.dimension
    assert fetched is repository.dimension
    assert page.items == (repository.dimension,)
    assert repository.create_call is not None
    code, value, audit = repository.create_call
    assert code.value == "MEM128"
    assert value == MemoryValue(amount=128, unit=MemoryUnit.GB)
    assert value.capacity_mb == 128_000
    assert audit.change_set_id == CHANGE_SET_ID
    assert audit.operations.dimension is DimensionOperation.CREATE
    assert audit.reason == "등록"
    assert repository.get_call == DIMENSION_ID
    assert repository.list_call == (cursor, 50)


@pytest.mark.unit
async def test_user_cannot_create_memory_dimension() -> None:
    repository = RecordingMemoryRepository()
    create = CreateMemory(
        repository,
        AuthorizationPolicy(),
        HumanMutationAuditFactory(change_set_ids=lambda: CHANGE_SET_ID),
    )

    with pytest.raises(AuthorizationDenied):
        await create.execute(
            _principal(UserRole.USER), code="MEM128", amount=128, unit="GB", reason=None
        )

    assert repository.create_call is None


@pytest.mark.unit
async def test_admin_updates_memory_amount_and_unit_as_one_value() -> None:
    repository = RecordingMemoryRepository()
    update = UpdateMemoryValue(
        repository,
        AuthorizationPolicy(),
        HumanMutationAuditFactory(change_set_ids=lambda: CHANGE_SET_ID),
    )

    await update.execute(
        _principal(UserRole.ADMIN),
        DIMENSION_ID,
        expected_version=3,
        amount=1,
        unit=" tb ",
        reason=" 용량 변경 ",
    )

    assert repository.update_call is not None
    dimension_id, version, value, audit, code = repository.update_call
    assert dimension_id == DIMENSION_ID
    assert version == 3
    assert value == MemoryValue(amount=1, unit=MemoryUnit.TB)
    assert code is None
    assert audit.operations.dimension is DimensionOperation.UPDATE
    assert audit.reason == "용량 변경"
